#!/bin/bash
# Must be provided as argument to the script
CONFIG_FILEPATH="$1"
OUTPUT_DIR="$2"
# Constants
PERTURBATIONS=("backtranslation" "duplication" "addition" "deletion" "replacement")
STRATEGIES=("random" "best-case" "worst-case")
PERTURBED_FRAC=(0.1 0.5 1.0)

# Run the baseline
sbatch scripts/run.sh "$CONFIG_FILEPATH" "$OUTPUT_DIR/baseline"

# Run the grid
for perturbation in "${PERTURBATIONS[@]}";
do
    for strategy in "${STRATEGIES[@]}";
    do
        # Sorting does not need to run for multiple perturbed fractions
        sbatch scripts/run.sh "$CONFIG_FILEPATH" \
            "$OUTPUT_DIR/perturbations/$strategy/sorting" \
            "sorting" \
            "$strategy"

        for perturbed_frac in "${PERTURBED_FRAC[@]}";
        do
            sbatch scripts/run.sh "$CONFIG_FILEPATH" \
                "$OUTPUT_DIR/perturbations/$strategy/$perturbation/$perturbed_frac" \
                "${perturbation}" \
                "$strategy" \
                "${perturbed_frac}"
        done
    done
done