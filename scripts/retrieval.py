import copy
from enum import Enum
from functools import partial
from pathlib import Path
from typing import List


import pyterrier as pt
import typer
from retrieval_exploration import indexing
from retrieval_exploration.common import util
from rich import print

app = typer.Typer()

# This controls the sizes of batches which are written to disk by HuggingFace Datasets.
# Smaller values save memory at the cost of more compute
_WRITER_BATCH_SIZE = 250


class HFDatasets(str, Enum):
    multinews = "multinews"
    multixscience = "multixscience"
    ms2 = "ms2"


class Retriever(str, Enum):
    sparse = "sparse"
    dense = "dense"
    sparse_re_ranker = "sparse+re-ranker"
    dense_re_ranker = "dense+re-ranker"


@app.command()
def main(
    hf_dataset_name: HFDatasets = typer.Argument(
        ..., case_sensitive=False, help="The name of a supported HuggingFace Dataset."
    ),
    dataset_dict_path: Path = typer.Argument(
        ...,
        help=(
            "Path (e.g. dataset/train) or remote URI (e.g. s3://my-bucket/dataset/train) of the dataset dict"
            " directory where the dataset dict will be saved to."
        ),
    ),
    index_path: Path = typer.Option(
        None,
        help=(
            "Directory to save the PyTerrier index. If an index already exists at this path and"
            " --overwrite-index is not passed, the index will be overwritten. If not provided, the index will be"
            " saved to util.CACHE_DIR / 'indexes'."
        ),
    ),
    retriever: Retriever = typer.Option(
        Retriever.sparse, case_sensitive=False, help="The type of retrieval pipeline to use."
    ),
    splits: List[str] = typer.Option(
        None, help="Which splits of the dataset to replace with retrieved documents. Defaults to all splits."
    ),
    overwrite_index: bool = typer.Option(False, help="Overwrite the PyTerrier index at --index-path, if it exists."),
    overwrite_cache: bool = typer.Option(
        False, help="Overwrite the cached copy of the HuggingFace dataset, if it exits."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Perform retrieval and report results without re-building the dataset. Useful for tuning and evaluation.",
    ),
) -> None:
    """Recreates the chosen HuggingFace dataset using the documents retrieved from an IR system."""

    # Any dataset specific setup goes here
    if hf_dataset_name == HFDatasets.multinews:
        pt_dataset = indexing.MultiNewsDataset()
        doc_sep_token = util.DOC_SEP_TOKENS[pt_dataset.path]

    elif hf_dataset_name == HFDatasets.multixscience:
        pt_dataset = indexing.MultiXScienceDataset()
        doc_sep_token = None

    elif hf_dataset_name == HFDatasets.ms2:
        pt_dataset = indexing.MS2Dataset()
        doc_sep_token = None

    # Create a directory to store the index if it wasn't provided
    index_path = Path(index_path) if index_path is not None else Path(util.CACHE_DIR) / "indexes" / pt_dataset.path
    if pt_dataset.name is not None:
        index_path = index_path / pt_dataset.name
    index_path.mkdir(parents=True, exist_ok=True)

    # Use all splits if not specified
    splits = splits or list(pt_dataset.keys())

    # Create a new copy of the dataset and replace its source documents with retrieved documents
    hf_dataset = copy.deepcopy(pt_dataset._hf_dataset)
    print(f"[bold green]:white_check_mark: Loaded the dataset from '{pt_dataset.info_url()}' [/bold green]")

    indexref = pt_dataset.get_index(str(index_path), overwrite=overwrite_index, verbose=True)
    # In general, we should always load the actual index
    # See: https://pyterrier.readthedocs.io/en/latest/terrier-retrieval.html#index-like-objects
    index = pt.IndexFactory.of(indexref)
    print(f"[bold green]:white_check_mark: Loaded the index from '{index_path}' [/bold green]")

    if retriever == Retriever.sparse:
        retrieval_pipeline = pt.BatchRetrieve(index, wmodel="BM25", metadata=["docno", "text"], verbose=True)
    elif retriever == Retriever.dense:
        raise NotImplementedError()
    elif retriever == Retriever.sparse_re_ranker:
        raise NotImplementedError
    elif retriever == Retriever.dense_re_ranker:
        raise NotImplementedError()
    print(f"[bold green]:white_check_mark: Loaded the '{retriever.value}' retrieval pipeline[/bold green]")

    for split in splits:
        # Use PyTerrier to actually perform the retrieval and then replace the source docs with the retrieved docs
        # See: https://pyterrier.readthedocs.io/en/latest/terrier-retrieval.html
        topics = pt_dataset.get_topics(split)
        qrels = pt_dataset.get_qrels(split)
        retrieved = retrieval_pipeline.transform(topics)

        print(f"[bold blue]:test_tube: Evaluating '{retriever}' retrieval pipeline on the '{split}' set [/bold blue]")
        print(
            pt.Experiment(
                [retrieved],
                topics=topics,
                qrels=qrels,
                eval_metrics=["ndcg", "recall_100", "recall_1000", "Rprec"],
                names=[retriever.value],
                round=4,
            )
        )

        if dry_run:
            print("[bold yellow]:warning: --dry-run argument provided, dataset will not be re-built[/bold yellow]")
            continue

        hf_dataset[split] = hf_dataset[split].map(
            partial(pt_dataset.replace, doc_sep_token=doc_sep_token, split=split, retrieved=retrieved),
            with_indices=True,
            load_from_cache_file=not overwrite_cache,
            writer_batch_size=_WRITER_BATCH_SIZE,
            desc=f"Re-building {split} set",
        )
        print(f"[bold blue]:repeat: Source documents in '{split}' set replaced with retrieved documents[/bold blue]")

    if not dry_run:
        hf_dataset.save_to_disk(dataset_dict_path)
        print(f"[bold green]:floppy_disk: Re-built dataset saved to {dataset_dict_path} [/bold green]")


if __name__ == "__main__":
    app()
