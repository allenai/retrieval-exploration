#!/bin/bash
# Requested resources
#SBATCH --mem=24G
#SBATCH --cpus-per-task=1
#SBATCH --gres=gpu:v100l:1
# Wall time and job details
#SBATCH --time=12:00:00
#SBATCH --job-name=train
#SBATCH --account=rrg-wanglab
# Emails me when job starts, ends or fails
#SBATCH --mail-user=johnmgiorgi@gmail.com
#SBATCH --mail-type=FAIL
# Use this command to run the same job interactively
# salloc --mem=12G --cpus-per-task=1 --gres=gpu:v100l:1 --time=3:00:00 --account=rrg-wanglab
# salloc --mem=12G --cpus-per-task=1 --gres=gpu:v100l:1 --time=3:00:00 --account=def-gbader

##### Environment #####
PROJECT_NAME="retrieval-exploration"
cd "$PROJECT/$USER/$PROJECT_NAME"

# Load the required modules and activate the environment
module load StdEnv/2020 gcc/9.3.0 python/3.8 cuda/11.4 arrow/7.0.0 java/11.0.2
poetry shell
# poetry shell should activate the virtual environment, but it doesn't seem to? So do it manually:
# See https://python-poetry.org/docs/basic-usage/#activating-the-virtual-environment for details.
source $(poetry env info --path)/bin/activate

##### Job #####
# This calls a modified version of the example summarization script from HF (with Trainer). For details,
# see: https://github.com/huggingface/transformers/tree/main/examples/pytorch/summarization#with-trainer

# There are many arguments and its easy to loose track, so we try to organize them as follows:
# - First, from the example script, starting with ModelArguments and ending with DataTrainingArguments
# - Then, Seq2SeqTrainingArguments. For details,
#   see: (https://huggingface.co/docs/transformers/main_classes/trainer#transformers.Seq2SeqTrainingArguments)
python "./scripts/index.py"