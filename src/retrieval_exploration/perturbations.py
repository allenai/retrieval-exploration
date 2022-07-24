import math
import random
import warnings
from functools import lru_cache
from itertools import zip_longest
from typing import List, Optional

import more_itertools
import nlpaug.augmenter.word as naw
import nltk
import sentence_transformers as st
import torch
from tqdm import tqdm

from retrieval_exploration.common import util

_SEMANTIC_SIMILARITY_MODEL = "all-MiniLM-L6-v2"
_BT_FROM_MODEL_NAME = "Helsinki-NLP/opus-mt-en-da"
_BT_TO_MODEL_NAME = "Helsinki-NLP/opus-mt-da-en"


@lru_cache(maxsize=None)
def _get_doc_embeddings(input_docs: List[str], embedder: st.SentenceTransformer) -> torch.Tensor:
    """Return a `torch.Tensor` containing the embeddings of `input_docs` obtained using the SentenceTransformer
    model `embedder`. The embeddings are cached, so that subsequent calls to this function with the same arguments
    will not re-compute them.
    """
    return embedder.encode(input_docs, batch_size=512, convert_to_tensor=True, normalize_embeddings=True)


class Perturber:
    def __init__(
        self, perturbation: str, doc_sep_token: str, strategy: str = "random", seed: Optional[int] = None
    ) -> None:
        """An object for applying document-level perturbations to some multi-document inputs.

        # Parameters

        perturbation : `str`
            The type of perturbation to apply.
        doc_sep_token : `str`
            The token that separates individual documents in the input strings.
        strategy : `str`, optional (default="random")
            The strategy to use for perturbation. Must be one of `"random"`, `"best-case"`, or `"worst-case"`.
        seed : `int`, optional (default=None)
            If provided, will locally set the seed of the `random` module with this value.

        Usage example:

        >>> inputs = ["document 1 <doc-sep> document 2 <doc-sep> document 3 <doc-sep> document 4"]
        >>> perturber = Perturber("deletion", doc_sep_token="<doc-sep>", strategy="random")
        >>> perturbed_inputs = perturber(inputs, perturbed_frac=0.1)
        """
        self._perturbation = perturbation

        # TODO: Some sort of registry would be better
        self._perturbation_func = getattr(self, self._perturbation, None)
        if self._perturbation_func is None:
            raise ValueError(f"Got an unexpected value for perturbation: {perturbation}")

        self._doc_sep_token = doc_sep_token
        self._strategy = strategy
        self._rng = random.Random(seed)

        device = "cuda" if torch.cuda.is_available() else "cpu"

        # Some perturbations require special components, like a backtranslation model
        self._aug = None
        if self._perturbation == "backtranslation":
            self._aug = naw.BackTranslationAug(
                from_model_name=_BT_FROM_MODEL_NAME,
                to_model_name=_BT_TO_MODEL_NAME,
                device=device,
                # We backtranslate on individual sentences, so this max_length should be plenty.
                max_length=256,
            )

        # Non-random strategies need an embedder for document selection
        self._embedder = None
        if self._strategy != "random":
            self._embedder = st.SentenceTransformer(_SEMANTIC_SIMILARITY_MODEL, device=device)

    def __call__(
        self,
        inputs: List[str],
        *,
        perturbed_frac: float = None,
        targets: Optional[List[str]] = None,
        documents: Optional[List[str]] = None,
    ) -> List[str]:
        """

        # Parameters

        inputs : `List[str]`
            A list of strings, each string containing the input documents for one example. It is assumed
            that documents are seperated by `doc_sep_token`. The returned list will be of the same length as
            `inputs` and contain the perturbed documents.
        perturbed_frac : `float`, optional (default=None)
            The percentage of documents in each example that should be perturbed. If not provided and selected
            perturbation is not `"sorting"`, returns `inputs` unchanged.
        targets : `List[str]`, optional (default=None)
            If provided, and `strategy` is not `"random"`, the input documents to perturb will be selected based on
            similarity or dissimilarity to these target documents, according to `strategy`. Must be the same
            length as `inputs`.
        documents : `List[str]`, optional (default=None)
            If provided, and `strategy` is not `"random"`, these documents will be considered (along with the
            document in `inputs`) for selection during perturbation. Has no effect if selected perturbation is not
            `"addition"` or `"replacement"`.
        """

        if targets is not None and len(inputs) != len(targets):
            raise ValueError(
                "If targets provided, then len(targets) must equal len(inputs)."
                f" Got len(targets)=={len(targets)} and len(inputs)={len(inputs)}."
            )

        if self._perturbation != "sorting" and not perturbed_frac:
            warnings.warn(
                f"perturbed_frac is falsey ({perturbed_frac}) and selected perturbation is not 'sorting'."
                " Inputs will be returned unchanged."
            )
            return inputs

        if documents is not None and self._perturbation not in ["addition", "replacement"]:
            warnings.warn(
                "documents provided, but perturbation is not 'addition' or 'replacement'. They will be ignored."
            )

        # Need an iterable, but an empty list as default value is bad practice
        targets = targets or []

        # All examples that can be considered for selection (ignoring duplicates)
        documents = inputs + documents if documents is not None else inputs
        documents = list(dict.fromkeys(documents))

        perturbed_inputs = []
        for example, target in tqdm(
            zip_longest(inputs, targets), desc="Perturbing inputs", total=max(len(inputs), len(targets))
        ):
            perturbed_example = self._perturbation_func(  # type: ignore
                example=example, target=target, perturbed_frac=perturbed_frac, documents=documents
            )
            perturbed_inputs.append(perturbed_example)

        return perturbed_inputs

    def backtranslation(
        self,
        *,
        example: str,
        perturbed_frac: float,
        target: Optional[str] = None,
        documents: Optional[List[str]] = None,
    ) -> str:

        input_docs = util.split_docs(example, doc_sep_token=self._doc_sep_token)
        num_docs = util.get_num_docs(example, doc_sep_token=self._doc_sep_token)

        # The absolute number of documents to perturb
        k = math.ceil(perturbed_frac * num_docs)

        # If we are backtranslating all documents, we do not need to sample
        if k == num_docs:
            sampled_docs = input_docs
        elif self._strategy == "random":
            sampled_docs = self._select_docs([example], k=k)
        else:
            sampled_docs = self._select_docs(
                documents=[example],
                k=k,
                target=target,
                largest=self._strategy == "worst-case",
            )

        # Back translate the sampled documents. To take advantage of batching, we will
        # collect the sentences of all documents, pass them to the model, and then unflatten them.
        unflattened_sents = [nltk.sent_tokenize(doc) for doc in sampled_docs]
        back_translated_sents = self._aug.augment(list(more_itertools.flatten(unflattened_sents)))  # type: ignore
        back_translated_docs = util.unflatten(
            back_translated_sents, lengths=[len(sents) for sents in unflattened_sents]
        )

        for sampled, translated in zip(sampled_docs, back_translated_docs):
            input_docs[input_docs.index(sampled)] = " ".join(sent.strip() for sent in translated)

        perturbed_example = f" {self._doc_sep_token} ".join(input_docs)

        return perturbed_example

    def sorting(
        self,
        *,
        example: str,
        perturbed_frac: Optional[float] = None,
        target: Optional[str] = None,
        documents: Optional[List[str]] = None,
    ) -> str:
        """Given `inputs`, a list of strings where each string contains the input documents seperated
        by `doc_sep_token` of one example from the dataset, perturbs the input by randomly shuffling the
        order of documents in each example.

        # Parameters

        inputs : `List[str]`
            A list of strings, each string containing the input documents for one example. It is assumed
            that documents are seperated by `doc_sep_token`.
        perturbed_frac : `float`, optional (default=None)
            Has no effect. Exists for consistency with other perturbation functions.
        seed : `int`, optional (default=None)
            If provided, will locally set the seed of the `random` module with this value.
        """

        input_docs = util.split_docs(example, doc_sep_token=self._doc_sep_token)

        if self._strategy == "random":
            self._rng.shuffle(input_docs)
        else:
            input_docs = self._select_docs(
                documents=[example],
                k=len(input_docs),
                target=target,
                largest=self._strategy == "best-case",
            )

        perturbed_example = f" {self._doc_sep_token} ".join(input_docs)
        return perturbed_example

    def duplication(
        self,
        *,
        example: str,
        perturbed_frac: float,
        target: Optional[str] = None,
        documents: Optional[List[str]] = None,
    ) -> str:
        """Given `inputs`, a list of strings where each string contains the input documents seperated
        by `doc_sep_token` of one example from the dataset, perturbs the input by replacing `perturbed_frac`
        percent of documents in each example with a random document sampled from `inputs.`

        # Parameters

        inputs : `List[str]`
            A list of strings, each string containing the input documents for one example. It is assumed
            that documents are seperated by `doc_sep_token`.
        doc_sep_token : `str`
            The token that separates individual documents in `inputs`.
        perturbed_frac : `float`, optional (default=None)
            The percentage of documents in each example that should be randomly replaced with a document
            sampled from `inputs`. If None (or falsey), no documents will be perturbed as this is a no-op.
        seed : `int`, optional (default=None)
            If provided, will locally set the seed of the `random` module with this value.
        """

        input_docs = util.split_docs(example, doc_sep_token=self._doc_sep_token)
        num_docs = util.get_num_docs(example, doc_sep_token=self._doc_sep_token)

        # The absolute number of documents to perturb
        k = math.ceil(perturbed_frac * num_docs)

        # If we are duplicating all documents, we do not need to sample
        if k == num_docs:
            repeaters = input_docs
        elif self._strategy == "random":
            repeaters = self._rng.sample(input_docs, k)
        else:
            repeaters = self._select_docs(
                documents=[example],
                k=k,
                target=target,
                largest=self._strategy == "best-case",
            )

        perturbed_example = f" {self._doc_sep_token} ".join(input_docs + repeaters)
        return perturbed_example

    def addition(
        self,
        *,
        example: str,
        perturbed_frac: float,
        documents: List[str],
        target: Optional[str] = None,
    ) -> str:
        """Given `inputs`, a list of strings where each string contains the input documents seperated
        by `doc_sep_token` of one example from the dataset, perturbs the input by adding `perturbed_frac`
        percent of documents in each example with a random document sampled from `inputs.`

        # Parameters

        inputs : `List[str]`
            A list of strings, each string containing the input documents for one example. It is assumed
            that documents are seperated by `doc_sep_token`.
        doc_sep_token : `str`
            The token that separates individual documents in `inputs`.
        perturbed_frac : `float`, optional (default=None)
            The percentage of documents in each example that should be randomly replaced with a document
            sampled from `inputs`. If None (or falsey), no documents will be perturbed as this is a no-op.
        seed : `int`, optional (default=None)
            If provided, will locally set the seed of the `random` module with this value.
        """

        input_docs = util.split_docs(example, doc_sep_token=self._doc_sep_token)
        num_docs = util.get_num_docs(example, doc_sep_token=self._doc_sep_token)

        # The absolute number of documents to perturb
        k = math.ceil(perturbed_frac * num_docs)

        if self._strategy == "random":
            sampled_docs = self._select_docs(documents, k=k, query=example)
        else:
            sampled_docs = self._select_docs(
                documents=documents,
                k=k,
                query=example,
                target=target,
                largest=self._strategy == "best-case",
            )

        perturbed_example = f" {self._doc_sep_token} ".join(input_docs + sampled_docs)
        return perturbed_example

    def deletion(
        self,
        *,
        example: str,
        perturbed_frac: float,
        target: Optional[str] = None,
        documents: Optional[List[str]] = None,
    ) -> str:
        """Given `inputs`, a list of strings where each string contains the input documents seperated
        by `doc_sep_token` of one example from the dataset, perturbs the input by removing `perturbed_frac`
        percent of documents in each example at random.

        # Parameters

        inputs : `List[str]`
            A list of strings, each string containing the input documents for one example. It is assumed
            that documents are seperated by `doc_sep_token`.
        doc_sep_token : `str`
            The token that separates individual documents in `inputs`.
        perturbed_frac : `float`, optional (default=None)
            The percentage of documents in each example that should be randomly replaced with a document
            sampled from `inputs`. If None (or falsey), no documents will be perturbed as this is a no-op.
        seed : `int`, optional (default=None)
            If provided, will locally set the seed of the `random` module with this value.
        """

        input_docs = util.split_docs(example, doc_sep_token=self._doc_sep_token)
        num_docs = util.get_num_docs(example, doc_sep_token=self._doc_sep_token)

        # The absolute number of documents to perturb
        k = math.ceil(perturbed_frac * num_docs)

        # If we are deleting all documents, just return the empty string.
        if k == num_docs:
            return ""

        if self._strategy == "random":
            to_delete = self._rng.sample(range(num_docs), k)
        else:
            sampled_docs = self._select_docs(
                documents=[example],
                k=k,
                target=target,
                largest=self._strategy == "worst-case",
            )
            to_delete = [input_docs.index(doc) for doc in sampled_docs]

        # Collect the perturbed example
        pertured_example = f" {self._doc_sep_token} ".join(
            doc for j, doc in enumerate(input_docs) if j not in to_delete
        )

        return pertured_example

    def replacement(
        self,
        *,
        example: str,
        perturbed_frac: float,
        documents: List[str],
        target: Optional[str] = None,
    ) -> str:

        input_docs = util.split_docs(example, doc_sep_token=self._doc_sep_token)
        num_docs = util.get_num_docs(example, doc_sep_token=self._doc_sep_token)

        # The absolute number of documents to perturb
        k = math.ceil(perturbed_frac * num_docs)

        if self._strategy == "random":
            sampled_docs = self._select_docs(documents, k=k, query=example)
            replace_indices = self._rng.sample(range(num_docs), k)

        else:
            # In the best case, replace the least similar documents with the most similar documents and vice versa
            # in the worst case.
            largest = self._strategy == "best-case"
            sampled_docs = self._select_docs(
                documents=documents,
                k=k,
                query=example,
                target=target,
                largest=largest,
            )
            to_replace = self._select_docs(
                documents=[example],
                k=k,
                target=target,
                largest=not largest,
            )
            replace_indices = [input_docs.index(doc) for doc in to_replace]

        for i, doc in zip(replace_indices, sampled_docs):
            input_docs[i] = doc.strip()

        perturbed_example = f" {self._doc_sep_token} ".join(input_docs)
        return perturbed_example

    def _select_docs(
        self,
        documents: List[str],
        *,
        k: int,
        query: Optional[str] = None,
        target: Optional[str] = None,
        largest: bool = True,
    ) -> List[str]:
        """Randomly samples `k` documents without replacement from `documents` according to `strategy`. Assumes
        that each string in `documents` contains one or more document separated by `doc_sep_token`. Any documents
        in `query`, which should be formatted similar to documents, will be excluded from selection.

        # Parameters

        documents : `List[str]`
            A list of strings to sample documents from. It is assumed that each string contains the input documents
            for one example, and that that items in this list are seperated by `doc_sep_token`.
        k : `int`
            The number of documents to sample (without replacement) from `documents`.
        query : `str`, optional (default=None)
            If provided, documents in `query` will not be sampled from `documents`. Documents will be selected
            based on similarity or dissimilarity to these documents, according to `strategy`. Should be provided
            in the same format as `documents`.
        target : `str`, optional (default=None)
            If provided, documents will be selected based on comparison to `target` instead of `query`.
        largest : `bool`
            If `True`, the top-k documents are returned, otherwise the bottom-k documents are returned.
        """
        if self._strategy != "random" and not query and not target:
            raise ValueError(
                "Must provide either a `query` or a `target` when using a `strategy` other than `random`."
            )
        if self._strategy == "random" and target is not None:
            warnings.warn("strategy is random, but target is not None. target will be ignored.")

        # Extract all individual documents
        documents = list(
            more_itertools.flatten(
                util.split_docs(example, doc_sep_token=self._doc_sep_token) for example in documents
            )
        )

        # If query is provided, remove it from the possible inputs
        if query is not None:
            query_docs = util.split_docs(query, doc_sep_token=self._doc_sep_token)
            documents = [doc for doc in documents if doc not in query_docs]

        # Check that we have enough documents to sample from
        if len(documents) < k:
            raise ValueError(f"Not enough documents to sample {k} without replacement. Only have {len(documents)}.")

        if self._strategy == "random":
            return self._rng.sample(documents, k)

        # Cache all inputs document embeddings to make this as fast as possible.
        doc_embeddings = _get_doc_embeddings(tuple(documents), embedder=self._embedder).to(
            self._embedder.device  # type: ignore
        )

        # If target is provided, look for docs most similar to it. Otherwise look for docs most similar to the query.
        if target:
            query_embedding = self._embedder.encode(  # type: ignore
                target, convert_to_tensor=True, normalize_embeddings=True
            )
            scores = st.util.dot_score(query_embedding, doc_embeddings)[0]
        else:
            query_embedding = self._embedder.encode(  # type: ignore
                query_docs, convert_to_tensor=True, normalize_embeddings=True
            )
            scores = st.util.dot_score(query_embedding, doc_embeddings)
            scores = torch.mean(scores, axis=0)

        # Return the the top k most similar (or dissimilar) documents
        indices = torch.topk(scores, k=k, largest=largest, sorted=True).indices
        return [documents[i] for i in indices]
