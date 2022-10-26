# CoRT: Contrastive Rhetorical Tagging

[[Competition Organizer]](https://aida.kisti.re.kr/contest/main/main.do)
[[Problem]](https://aida.kisti.re.kr/contest/main/problem/PROB_000000000000017/detail.do)

The model is built for AI/ML modeling competition hosting from [KISTI](https://www.kisti.re.kr) (Korea Institute of Science and Technology Information).
The main problem of this model is classifying sentences from research papers written in Korean that had been tagged based on rhetorical meaning.

### Problem Solving

The problem have following hierarchical categories.

- Research purpose
  - Problem definition
  - Hypothesis
  - Technology definition
- Research method
  - Suggestion
  - The target data
  - Data processing
  - Theory / models
- Research result
  - Performance / effects
  - Follow-up research

To solve the problem effectively, I have decided to train the model in **Contrastive Learning** manner.
You can use following Pre-trained models: **KorSci-BERT**, **KorSci-ELECTRA**, and **other BERT, ELECTRA, RoBERTa based models from Hugging Face.**

I have used `klue/roberta-base` for additional pre-trained model.

##### Supervised Contrastive Learning (The best in my case)

[[arXiv]](https://arxiv.org/abs/2004.11362) - Supervised Contrastive Learning

The classic contrastive learning is Self-supervised Learning, the model can classify between different objects, but struggling with classifying objects in same label.<br>
In the paper, they suggest supervised-manner learning when you have labels.

I've used contrastive loss from the paper, and Pre-training and Fine-tuning separation.<br>
1. Perform Pre-training in representation learning manner.
2. to perform Fine-tuning, cut off the representation projection layer and attach new classifier layers.

This gave me significant improvement on performance and speed of converge.

##### Margin-based Contrastive Learning

[[arXiv]](https://arxiv.org/abs/2104.08812) - Contrastive Out-of-Distribution Detection for Pretrained Transformers

Contrastive Representation Learning is powerful enough, but pushing all of labels each other may not be that easy.<br>
Maximizing margin between representations is very helpful on clarifying decision boundaries between representations.<br>
Although the paper suggest this for out-of-distribution problem, but experimenting clarifying decision boundaries in other tasks is reasonable.

##### Hierarchical Contrastive Learning

[[arXiv]](https://arxiv.org/abs/2204.13207) - Use All The Labels: A Hierarchical Multi-Label Contrastive Learning Framework

Since the dataset have sub-depth categories, I thought the model can learn about relationships between top-level categories, and between sub-depth categories at the same time.<br>
The paper suggests training strategy, by pulling together in the same-level categories and pulling stronger when the level is lower and lower.

### Prerequisites

All prerequisites must be up-to-date.
W&B is always required to run pre-training and fine-tuning scripts.
Requiring to use Python 3.8 or above.

```bash
- tensorflow
- tensorflow_addons
- torch
- transformers
- scikit-learn
- wandb
- pandas
- konlpy 
- soynlp
```

### Pre-training

W&B Sweeps configurations are available in `./sweeps` directory.<br>
Run automatic hyperparameter tuning by (for example) `wandb sweep ./sweeps/pretraining_supervised.yaml`<br>
And run `wandb agent "{entity_name}/CoRT Pre-training/{sweep_id}"`

Use `build_pretraining_data.py` to create a pre-training dataset from raw texts.
It has the following arguments:
- `--filepath`: Location of raw text dataset available at [KISTI](https://doi.org/10.23057/36).
- `--model_name`: Model name to be used as Pre-trained backbones. `korscibert` and `korscielectra` is available by default.
- `--output_dir`: Destination directory path to write out the tfrecords.
- `--korscibert_vocab`: Location of KorSci-BERT vocabulary file. (optional)
- `--korscielectra_vocab`: Location of KorSci-ELECTRA vocabulary file. (optional)
- `--num_processes`: Parallelize tokenization across multi processes. (4 as default)
- `--num_k_fold`: Number of K-Fold splits. (10 as default)
- `--test_size`: Rate of testing dataset. (0.0 as default)
- `--seed`: Seed of random state. (42 as default)

Use `run_pretraining.py` to pre-train the backbone model in representation learning manner.
It has the following arguments:
- `--gpu`: GPU to be utilized for training. ('all' as default, must be int otherwise)
- `--batch_size`: Size of the mini-batch. (64 as default)
- `--learning_rate`: Learning rate. (1e-3 as default)
- `--lr_fn`: Learning rate scheduler type. ('cosine_decay' as default. 'constant', 'cosine_decay', 'polynomial_decay', 'linear_decay' is available)
- `--weight_decay`: Rate of weight decay. (1e-6 as default)
- `--warmup_rate`: Rate of learning rate warmup on beginning. (0.06 as default. the total warmup steps is `int(num_train_steps * warmup_rate)`)
- `--repr_size`: Size of representation projection layer units. (1024 as default)
- `--gradient_accumulation_steps`: Multiplier for gradient accumulation. (1 as default)
- `--model_name`: Model name to be used as Pre-trained backbones.
- `--num_train_steps`: Total number of training steps. (10000 as default)
- `--loss_base`: Name of loss function for contrastive learning. ('margin' as default. 'margin', 'supervised' and 'hierarchical' is available)

The Pre-training takes 3 ~ 4 hours to complete on `NVIDIA A100`

### Fine-tuning

When pre-training is completed, all checkpoints would be located in `pretraining-checkpoints/{wandb_run_id}`

Use `run_finetuning.py` to fine-tune the pre-trained models.
It has the following arguments:
- `--gpu`: GPU to be utilized for training. ('all' as default, must be int otherwise)
- `--batch_size`: Size of the mini-batch. (64 as default)
- `--learning_rate`: Learning rate. (1e-3 as default)
- `--lr_fn`: Learning rate scheduler type. ('cosine_decay' as default. 'constant', 'cosine_decay', 'polynomial_decay', 'linear_decay' is available)
- `--weight_decay`: Rate of weight decay. (1e-6 as default. I recommend to use 0 when fine-tune)
- `--warmup_rate`: Rate of learning rate warmup on beginning. (0.06 as default. the total warmup steps is `int(epochs * steps_per_epoch * warmup_rate)`)
- `--repr_size`: Size of classifier dense layer. (1024 as default)
- `--model_name`: Model name to beu sed as Pre-trained backbones.
- `--pretraining_run_name`: W&B Run ID in `pretraining-checkpoints`. The pre-trained checkpoint model must be same with `--model_name` model.
- `--epochs`: Number of training epochs. (10 as default)
- `--repr_act`: Activation function name to be used after classifier dense layer. ('tanh' as default. 'none', and other name of activations supported from TensorFlow is available)
- `--loss_base`: Name of loss function for contrastive learning. ('margin' as default. 'margin', 'supervised' and 'hierarchical' is available)
- `--restore_checkpoint`: Name of checkpoint file. (`None` as default. I recommend 'latest' when fine-tune)
- `--repr_classifier`: Type of classification head. ('seq_cls' as default. 'seq_cls' and 'bi_lstm' is available)
- `--repr_preact`: Boolean to use pre-activation when activating representation logits. (`True` as default)
- `--train_at_once`: Boolean when you want to train the model from scratch without pre-training. (`False` as default)
- `--repr_finetune`: Boolean when you want to fine-tune the model with additional Representation Learning. (`False` as default) 
- `--include_sections`: Boolean when you want to use 'representation logits of sections' on label representation logits. (`False` as default. `--repr_finetune True` is required for this)


### Notes

I don't recommend to use KorSci-ELECTRA because of too high `[UNK]` token rate (about 85.2%).

| Model             | Number of [UNK] | Total Tokens | [UNK] Rate   |
|-------------------|-----------------|--------------|--------------|
| klue/roberta-base | 2,734           | 9,269,131    | 0.000295     |
| KorSci-BERT       | 14,237          | 9,077,386    | 0.001568     |
| KorSci-ELECTRA    | 7,345,917       | 8,621,489    | **0.852047** |



### Results

Results are not yet available because the competition is not finished.
