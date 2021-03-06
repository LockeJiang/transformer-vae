"""
    Train Transformer-VAEs using the Huggingface Trainer with Weights and Biasis.
"""
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

from datasets import load_dataset
import transformers
from transformers import (
    AutoTokenizer,
    HfArgumentParser,
    TrainingArguments,
    Adafactor,
    set_seed,
)
from transformers.integrations import is_wandb_available
from transformers.trainer_utils import is_main_process

from transformer_vae.trainer import VAE_Trainer
from transformer_vae.data_collator import DataCollatorForLanguageAutoencoding
from transformer_vae.trainer_callback import TellModelGlobalStep
from transformer_vae.model import T5_VAE_Model, Funnel_VAE_Model, Funnel_T5_VAE_Model, Funnel_gpt2_VAE_Model
from transformer_vae.sequence_checks import SEQ_CHECKS
from transformer_vae.config import T5_VAE_Config, Funnel_VAE_Config, Funnel_T5_VAE_Config, Funnel_gpt2_VAE_Config


logger = logging.getLogger(__name__)


CONFIG = {"t5": T5_VAE_Config, "funnel": Funnel_VAE_Config, "funnel-t5": Funnel_T5_VAE_Config, 'funnel-gpt2': Funnel_gpt2_VAE_Config}
MODEL = {"t5": T5_VAE_Model, "funnel": Funnel_VAE_Model, "funnel-t5": Funnel_T5_VAE_Model, 'funnel-gpt2': Funnel_gpt2_VAE_Model}
DEFAULT_TRANSFORMER_NAME = {
    "t5": "t5-base",
    "funnel": "funnel-transformer/intermediate",
    "funnel-t5": "funnel-transformer/intermediate",
}


@dataclass
class VAE_TrainingArguments(TrainingArguments):
    """
    Extra arguments to specify generation during evaluation.
    """

    generate_min_len: int = field(
        default=1,
        metadata={"help": "The minimum length of sequences to be generated from latent points during evaluation."},
    )
    generate_max_len: int = field(
        default=20,
        metadata={"help": "The maximum length of sequences to be generated from latent points during evaluation."},
    )
    n_random_samples: int = field(
        default=10,
        metadata={"help": "Number of random latent codes to sample from during evaluation."},
    )
    seq_check: str = field(
        default=None,
        metadata={"help": f"Run check on sequences from random latent codes. Options: {', '.join([str(k) for k in SEQ_CHECKS.keys()])}"},
    )
    max_validation_size: int = field(
        default=None,
        metadata={"help": "Limit the eval dataset size, defaults to not limiting it, must be < validation size."},
    )
    use_adafactor: bool = field(
        default=False,
        metadata={"help": "Whether to use one the Adafactor optimizer."},
    )
    sample_from_latent: bool = field(
        default=False,
        metadata={"help": "Whether to sample from the latent space during evaluation."},
    )
    test_classification: bool = field(
        default=False,
        metadata={"help": "Test using latent codes for unsupervised classification."},
    )


@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune, or train from scratch.
    """

    transformer_type: Optional[str] = field(
        default="t5",
        metadata={"help": f"The transfromer type to base the VAE on. Only {', '.join(CONFIG.keys())} supported."},
    )
    model_path: Optional[str] = field(
        default=None,
        metadata={
            "help": "The model checkpoint for weights initialization. Leave None if you want to train a model from scratch."
        },
    )
    transformer_name: Optional[str] = field(
        default=None,
        metadata={
            "help": "Name of the transformer model being using for encoding & decoding (only `t5` & `funnel` transfromer type supported)."
        },
    )
    transformer_decoder_name: Optional[str] = field(
        default="t5-base",
        metadata={
            "help": "Name of the transformer model being using for decoding the funnel transformer (only `t5` type transformers supported)."
        },
    )
    config_path: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config path if not the same as model_name"}
    )
    tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"}
    )
    cache_dir: Optional[str] = field(
        default=None, metadata={"help": "Where do you want to store the pretrained models downloaded from s3"}
    )
    use_fast_tokenizer: bool = field(
        default=True,
        metadata={"help": "Whether to use one of the fast tokenizer (backed by the tokenizers library) or not."},
    )
    latent_size: int = field(default=1_000, metadata={"help": "The size of the VAE's latent space."})
    set_seq_size: int = field(default=None, metadata={"help": "Set sequence size, must be set for some autoencoders."})
    encoded_seq_size: int = field(
        default=None, metadata={"help": "Sequence size of encoded sequence, needed for Funnel-VAE."}
    )
    encoder_model: Optional[str] = field(
        default=None, metadata={"help": "Name of the model that converts hidden states into latent codes."}
    )
    decoder_model: Optional[str] = field(
        default=None, metadata={"help": "Name of the model that converts latent codes into hidden states."}
    )
    # Arguments used during training
    n_previous_latent_codes: int = field(
        default=0,
        metadata={
            "help": "Use N previous batches of latent codes when calculating MMD loss, required when using small batches."
        },
    )
    mmd_batch_size: int = field(
        default=None,
        metadata={
            "help": "Run multuple, smaller batches for MMD-VAE regularisation loss (training batch size must be divisible by the MMD batch size)."
        },
    )
    dont_use_reg_loss: bool = field(
        default=False,
        metadata={
            "help": "Toggle regularisation loss, without regularisation your training an autoencoder rather than a VAE."
        },
    )
    reg_schedule_k: float = field(
        default=0.0025,
        metadata={"help": "Multiplied by global_step in a sigmoid, more gradually increase regulariser loss weight."},
    )
    reg_schedule_b: float = field(
        default=6.25,
        metadata={"help": "Added to global step in sigmoid, further delays increase in regulariser loss weight."},
    )
    skip_schedule_k: float = field(
        default=0.0006,
        metadata={"help": "If using a skip connection, gradually zero the connection."},
    )
    skip_schedule_b: float = field(
        default=11,
        metadata={"help": "If using a skip connection, gradually zero the connection."},
    )
    n_latent_tokens: int = field(
        default=None,
        metadata={
            "help": "Number of latent tokens to use for full sequence VAE (set to sequence length if its shorter), used with `encoder_model n-tokens`."
        },
    )
    use_skip_connection: bool = field(
        default=False,
        metadata={
            "help": "Add the encoders last full-length layer to the upsampled one. Only used with `Funnel encoder`."
        },
    )
    use_latent_dropout: bool = field(
        default=False,
        metadata={
            "help": "Start training by using all encoding tokens as latent, gradually dropout hidden units till only a small latent code is left."
        },
    )
    max_latent_dropout_rate: float = field(
        default=0.8,
        metadata={"help": "If using latent dropout, max dropout rate during training."},
    )
    latent_dropout_schedule_k: float = field(
        default=0.0009,
        metadata={"help": "If using latent dropout, gradually increase the dropout rate until at max_latent_dropout_rate."},
    )
    latent_dropout_schedule_b: float = field(
        default=11,
        metadata={"help": "If using latent dropout, gradually increase the dropout rate until at max_latent_dropout_rate."},
    )


@dataclass
class DataTrainingArguments:
    """
    Arguments pertaining to what data we are going to input our model for training and eval.
    """

    dataset_name: Optional[str] = field(
        default=None, metadata={"help": "The name of the dataset to use (via the datasets library)."}
    )
    dataset_config_name: Optional[str] = field(
        default=None, metadata={"help": "The configuration name of the dataset to use (via the datasets library)."}
    )
    text_column: Optional[str] = field(default=None, metadata={"help": "Use this dataset column as 'text'."})
    train_file: Optional[str] = field(default=None, metadata={"help": "The input training data file (a text file)."})
    validation_file: Optional[str] = field(
        default=None,
        metadata={"help": "An optional input evaluation data file to evaluate the perplexity on (a text file)."},
    )
    overwrite_cache: bool = field(default=False, metadata={"help": "Overwrite the cached training and evaluation sets"})
    preprocessing_num_workers: Optional[int] = field(
        default=None,
        metadata={"help": "The number of processes to use for the preprocessing."},
    )
    mlm_probability: float = field(
        default=0.0, metadata={"help": "Ratio of tokens to mask for masked language modeling loss"}
    )
    validation_name: str = field(
        default="validation",
        metadata={"help": "Name of the set to run evaluation on."},
    )
    classification_column: str = field(
        default="class_label",
        metadata={"help": "Test SVM classification using latent codes."},
    )
    num_classes: int = field(
        default=None,
        metadata={"help": "How many classes in the data, found using a ClassLabel column if none given."},
    )

    def __post_init__(self):
        if self.dataset_name is None and self.train_file is None and self.validation_file is None:
            raise ValueError("Need either a dataset name or a training/validation file.")
        else:
            if self.train_file is not None:
                extension = self.train_file.split(".")[-1]
                assert extension in ["csv", "json", "txt"], "`train_file` should be a csv, a json or a txt file."
            if self.validation_file is not None:
                extension = self.validation_file.split(".")[-1]
                assert extension in ["csv", "json", "txt"], "`validation_file` should be a csv, a json or a txt file."


def check_seq_size(tokenizer, text_column_name, data_args, datasets, set_seq_size):
    def tokenize_function(examples):
        return tokenizer(examples[text_column_name], padding="longest", truncation=True)

    tokenized_datasets = datasets.map(
        tokenize_function,
        batched=True,
        batch_size=1_000,
        num_proc=data_args.preprocessing_num_workers,
        remove_columns=[text_column_name],
        load_from_cache_file=not data_args.overwrite_cache,
    )

    max_seq_size = max(
        max([len(row) for row in tokenized_datasets["train"]["input_ids"][::1_000]]),
        max([len(row) for row in tokenized_datasets[data_args.validation_name]["input_ids"][::1_000]]),
    )

    if max_seq_size > set_seq_size:
        logger.warn(
            "Model has too short a sequence size for dataset, run with truncating & joining examples.\n"
            f"Dataset max text column size: {max_seq_size} Model max sequence size: {set_seq_size}"
        )
    elif set_seq_size > max_seq_size:
        logger.info(
            "Model can handle larger sequence size than present in the dataset.\n"
            f"Dataset max text column size: {max_seq_size} Model max sequence size: {set_seq_size}"
        )


def get_args():
    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, VAE_TrainingArguments))
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file,
        # let's parse it to get our arguments.
        model_args, data_args, training_args = parser.parse_json_file(json_file=os.path.abspath(sys.argv[1]))
    else:
        model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    # use train batch size if eval is default
    training_args.per_device_eval_batch_size = (
        training_args.per_device_train_batch_size
        if training_args.per_device_eval_batch_size == 8
        else training_args.per_device_eval_batch_size
    )

    if model_args.transformer_name is None:
        model_args.transformer_name = DEFAULT_TRANSFORMER_NAME[model_args.transformer_type]

    if model_args.encoded_seq_size is None and "funnel" not in model_args.transformer_type:
        model_args.encoded_seq_size = model_args.set_seq_size

    if (
        os.path.exists(training_args.output_dir)
        and os.listdir(training_args.output_dir)
        and training_args.do_train
        and not training_args.overwrite_output_dir
    ):
        raise ValueError(
            f"Output directory ({training_args.output_dir}) already exists and is not empty."
            "Use --overwrite_output_dir to overcome."
        )

    return model_args, data_args, training_args


def setup_logs(training_args):
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO if is_main_process(training_args.local_rank) else logging.WARN,
    )

    # Log on each process the small summary:
    logger.warning(
        f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}"
        + f", distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}"
    )
    # Set the verbosity to info of the Transformers logger (on main process only):
    if is_main_process(training_args.local_rank):
        transformers.utils.logging.set_verbosity_info()
    logger.info("Training/evaluation parameters %s", training_args)


def get_datasets(data_args):
    # Get the datasets: you can either provide your own CSV/JSON/TXT training and evaluation files (see below)
    # or just provide the name of one of the public datasets available on the hub at https://huggingface.co/datasets/
    # (the dataset will be downloaded automatically from the datasets Hub).
    #
    # For CSV/JSON files, this script will use the column called 'text' or the first column if no column called
    # 'text' is found. You can easily tweak this behavior (see below).
    #
    # In distributed training, the load_dataset function guarantee that only one local process can concurrently
    # download the dataset.
    if data_args.dataset_name is not None:
        # Downloading and loading a dataset from the hub.
        return load_dataset(data_args.dataset_name, data_args.dataset_config_name)
    data_files = {}
    if data_args.train_file is not None:
        data_files["train"] = data_args.train_file
    if data_args.validation_file is not None:
        data_files[data_args.validation_name] = data_args.validation_file
    extension = data_args.train_file.split(".")[-1]
    if extension == "txt":
        extension = "text"
    # TODO have this dataset split samples on some substring
    return load_dataset(extension, data_files=data_files)
    # See more about loading any type of standard or custom dataset (from files, python dict, pandas DataFrame, etc) at
    # https://huggingface.co/docs/datasets/loading_datasets.html.


def load_model_and_tokenizer(model_args):
    # Distributed training:
    # The `.from_pretrained` methods guarantee that only one local process can concurrently
    # download model & vocab.
    if model_args.config_path:
        config = CONFIG[model_args.transformer_type].from_pretrained(
            model_args.config_path, cache_dir=model_args.cache_dir
        )
    elif model_args.model_path:
        config = CONFIG[model_args.transformer_type].from_pretrained(
            model_args.model_path, cache_dir=model_args.cache_dir
        )
    else:
        config = CONFIG[model_args.transformer_type](
            latent_size=model_args.latent_size,
            transformer_name=model_args.transformer_name,
            transformer_decoder_name=model_args.transformer_decoder_name,
            encoder_model=model_args.encoder_model,
            decoder_model=model_args.decoder_model,
            set_seq_size=model_args.set_seq_size,
            encoded_seq_size=model_args.encoded_seq_size,
            n_previous_latent_codes=model_args.n_previous_latent_codes,
            mmd_batch_size=model_args.mmd_batch_size,
            use_reg_loss=(not model_args.dont_use_reg_loss),
            reg_schedule_k=model_args.reg_schedule_k,
            reg_schedule_b=model_args.reg_schedule_b,
            skip_schedule_k=model_args.skip_schedule_k,
            skip_schedule_b=model_args.skip_schedule_b,
            n_latent_tokens=model_args.n_latent_tokens,
            use_extra_logs=is_wandb_available(),
            use_skip_connection=model_args.use_skip_connection,
            use_latent_dropout=model_args.use_latent_dropout,
            max_latent_dropout_rate=model_args.max_latent_dropout_rate,
            latent_dropout_schedule_k=model_args.latent_dropout_schedule_k,
            latent_dropout_schedule_b=model_args.latent_dropout_schedule_b,
        )
        logger.warning("You are instantiating a new config instance from scratch (still using T5 checkpoint).")

    if model_args.tokenizer_name:
        tokenizer = AutoTokenizer.from_pretrained(
            model_args.tokenizer_name, cache_dir=model_args.cache_dir, use_fast=model_args.use_fast_tokenizer
        )
        if 'gpt' in model_args.tokenizer_name:
            tokenizer.pad_token = tokenizer.eos_token
    elif model_args.model_path:
        tokenizer = AutoTokenizer.from_pretrained(
            model_args.model_path, cache_dir=model_args.cache_dir, use_fast=model_args.use_fast_tokenizer
        )
    elif model_args.transformer_name:
        tokenizer = AutoTokenizer.from_pretrained(
            model_args.transformer_name, cache_dir=model_args.cache_dir, use_fast=model_args.use_fast_tokenizer
        )
    else:
        raise ValueError(
            "You are instantiating a new tokenizer from scratch. This is not supported by this script."
            "You can do it from another script, save it, and load it from here, using --tokenizer_name."
        )

    if model_args.model_path:
        model = MODEL[model_args.transformer_type].from_pretrained(
            model_args.model_path,
            from_tf=bool(".ckpt" in model_args.model_path),
            config=config,
            cache_dir=model_args.cache_dir,
        )
    else:
        logger.info("Training new model from scratch")
        model = MODEL[model_args.transformer_type](config)

    model.resize_token_embeddings(len(tokenizer))
    if model_args.set_seq_size:
        tokenizer.model_max_length = model_args.set_seq_size
    tokenizer.mask_token = tokenizer.unk_token

    return model, tokenizer


def preprocess_datasets(training_args, data_args, model_args, tokenizer, datasets):
    # Add class_label if needed
    if training_args.test_classification:
        if data_args.classification_column != "class_label":

            if not data_args.num_classes:
                data_args.num_classes = (
                    datasets[data_args.validation_name].features[data_args.classification_column].num_classes
                )

            def add_class_column(examples):
                return {"class_label": examples[data_args.classification_column]}

            datasets = datasets.map(add_class_column, remove_columns=[data_args.classification_column])

    # tokenize all the texts.
    if training_args.do_train:
        column_names = datasets["train"].column_names
    else:
        column_names = datasets[data_args.validation_name].column_names
    if data_args.text_column is not None:
        text_column_name = data_args.text_column
    else:
        text_column_name = "text" if "text" in column_names else column_names[0]

    if text_column_name != "text":
        logger.info(f'Using column "{text_column_name}" as text column.')

    if model_args.set_seq_size:
        check_seq_size(tokenizer, text_column_name, data_args, datasets, model_args.set_seq_size)

    def tokenize_function(examples):
        return tokenizer(examples[text_column_name], padding="max_length", truncation=True)

    tokenized_datasets = datasets.map(
        tokenize_function,
        batched=True,
        batch_size=None,  # apply tokenize_function to whole dataset at once (ensures longest length is global)
        num_proc=data_args.preprocessing_num_workers,
        remove_columns=[text_column_name],
        load_from_cache_file=not data_args.overwrite_cache,
    )

    if training_args.max_validation_size:
        tokenized_datasets[data_args.validation_name] = tokenized_datasets[data_args.validation_name].train_test_split(
            training_args.max_validation_size
        )["test"]

    data_collator = DataCollatorForLanguageAutoencoding(tokenizer=tokenizer, mlm_probability=data_args.mlm_probability)

    return data_collator, tokenized_datasets


def get_optimizers(training_args, model):
    optimizers = [None, None]
    if training_args.use_adafactor:
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
                "weight_decay": training_args.weight_decay,
            },
            {
                "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
            },
        ]
        optimizers[0] = Adafactor(
            optimizer_grouped_parameters, lr=training_args.learning_rate, scale_parameter=False, relative_step=False
        )
    return tuple(optimizers)


def main():
    model_args, data_args, training_args = get_args()

    setup_logs(training_args)

    # Set seed before initializing model.
    set_seed(training_args.seed)

    datasets = get_datasets(data_args)

    model, tokenizer = load_model_and_tokenizer(model_args)

    data_collator, tokenized_datasets = preprocess_datasets(training_args, data_args, model_args, tokenizer, datasets)

    # Initialize our Trainer
    trainer = VAE_Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized_datasets["train"] if training_args.do_train else None,
        eval_dataset=tokenized_datasets[data_args.validation_name] if training_args.do_eval else None,
        tokenizer=tokenizer,
        data_collator=data_collator,
        callbacks=[TellModelGlobalStep],
        optimizers=get_optimizers(training_args, model),
    )
    trainer.log({})

    # Training
    if training_args.do_train:
        trainer.train(
            model_path=model_args.model_path if model_args.model_path and os.path.isdir(model_args.model_path) else None
        )
        trainer.save_model()  # Saves the tokenizer too for easy upload

    # Evaluation
    results = {}
    if training_args.do_eval:
        logger.info("*** Evaluate ***")

        results = trainer.evaluate()

        output_eval_file = os.path.join(training_args.output_dir, "eval_results_T5_VAE.txt")
        if trainer.is_world_process_zero():
            with open(output_eval_file, "w") as writer:
                logger.info("***** Eval results *****")
                for key, value in results.items():
                    logger.info(f"  {key} = {value}")
                    writer.write(f"{key} = {value}\n")

    return results


def _mp_fn(index):
    # For xla_spawn (TPUs)
    main()
