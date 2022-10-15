import os
import math
import copy
import wandb
import logging
import collections
import numpy as np
import tensorflow as tf

from utils import utils
from utils.formatting_utils import setup_formatter
from cort.config import Config
from cort.datagen import CortDataGenerator
from cort.modeling_legacy import CortModel, CortForElaboratedRepresentation
from cort.optimization import GradientAccumulator, create_optimizer
from cort.preprocessing import parse_and_preprocess_sentences, normalize_texts, run_multiprocessing_job
from tensorflow.keras import callbacks, metrics, utils
from tensorflow.python.framework import smart_cond
from tensorflow_addons import metrics as metrics_tfa
from sklearn.model_selection import StratifiedKFold
from sklearn.utils.class_weight import compute_class_weight


def preprocess_sentences_on_batch(batch):
    sentences = []
    for sentence in batch:
        sentence = normalize_texts(sentence)
        sentences.append(sentence)
    return sentences


def setup_datagen(config: Config, tokenizer):
    df = parse_and_preprocess_sentences(config.train_path)

    # preprocess in multiprocessing manner
    results = run_multiprocessing_job(preprocess_sentences_on_batch, df['sentences'],
                                      num_processes=config.num_processes)
    sentences = []
    for sentences_batch in results:
        sentences += sentences_batch

    if config.dynamic_datagen:
        input_ids = np.array(sentences, dtype=np.object)
    else:
        tokenized = tokenizer(sentences,
                              padding='max_length',
                              truncation=True,
                              return_attention_mask=False,
                              return_token_type_ids=False)
        input_ids = tokenized['input_ids']
        input_ids = np.array(input_ids, dtype=np.int32)

    sections = df['code_sections'].values
    labels = df['code_labels'].values

    sections = np.array(sections, dtype=np.int32)
    labels = np.array(labels, dtype=np.int32)
    return input_ids, (sections, labels)


def splits_into_fold(config: Config, tokenizer, input_ids, labels):
    sections, labels = labels
    fold = StratifiedKFold(n_splits=config.num_k_fold, shuffle=True, random_state=config.seed)
    for index, (train_indices, valid_indices) in enumerate(fold.split(input_ids, labels)):
        if index != config.current_fold:
            continue

        train_input_ids = input_ids[train_indices]
        train_sections = sections[train_indices]
        train_labels = labels[train_indices]
        valid_input_ids = input_ids[valid_indices]
        valid_sections = sections[valid_indices]
        valid_labels = labels[valid_indices]

        steps_per_epoch = len(train_input_ids) // config.batch_size // config.gradient_accumulation_steps
        sections_cw = compute_class_weight('balanced', classes=np.unique(train_sections), y=train_sections)
        sections_cw = dict(enumerate(sections_cw))
        labels_cw = compute_class_weight('balanced', classes=np.unique(train_labels), y=train_labels)
        labels_cw = dict(enumerate(labels_cw))

        if config.dynamic_datagen:
            training = CortDataGenerator(
                config, tokenizer,
                train_input_ids, train_sections, train_labels,
                steps_per_epoch=len(train_input_ids) // config.batch_size,  # irrespective gradient accumulation steps
                shuffle=True
            )
            validation = CortDataGenerator(
                config, tokenizer,
                valid_input_ids, valid_sections, valid_labels,
                steps_per_epoch=int(math.ceil(len(valid_input_ids) / config.batch_size)),
                shuffle=False
            )
        else:
            training = (train_input_ids, (train_sections, train_labels))
            validation = (valid_input_ids, (valid_sections, valid_labels))

        logging.info('Class weights:')
        logging.info('- Sections:')
        for i in range(config.num_sections):
            logging.info('  - Section #{}: {}'.format(i, sections_cw[i]))
        logging.info('- Labels:')
        for i in range(config.num_labels):
            logging.info('  - Label #{}: {}'.format(i, labels_cw[i]))

        FoldedDatasetOutput = collections.namedtuple('FoldedDatasetOutput', [
            'training', 'validation', 'steps_per_epoch',
            'sections_cw', 'labels_cw'
        ])
        return FoldedDatasetOutput(training=training, validation=validation,
                                   steps_per_epoch=steps_per_epoch,
                                   sections_cw=sections_cw, labels_cw=labels_cw)

    raise ValueError('Invalid current fold number: {} out of total {} folds'
                     .format(config.current_fold, config.num_k_fold))


def class_weight_map_fn(sections_cw, labels_cw):
    def _calc_cw_tensor(cw):
        class_ids = list(sorted(cw.keys()))
        expected_class_ids = list(range(len(class_ids)))
        if class_ids != expected_class_ids:
            raise ValueError((
                'Expected `class_weight` to be a dict with keys from 0 to one less'
                'than the number of classes, found {}'.format(cw)
            ))

        return tf.convert_to_tensor([cw[int(c)] for c in class_ids])

    sections_cw_tensor = _calc_cw_tensor(sections_cw)
    labels_cw_tensor = _calc_cw_tensor(labels_cw)

    @tf.function
    def _rearrange_cw(labels, cw_tensor):
        y_classes = smart_cond.smart_cond(
            labels.shape.rank == 2 and tf.shape(labels)[1] > 1,
            lambda: tf.argmax(labels, axis=1),
            lambda: tf.cast(tf.reshape(labels, (-1,)), dtype=tf.int32)
        )
        cw = tf.gather(cw_tensor, y_classes)
        return cw

    @tf.function
    def map_fn(input_ids, labels):
        sections, labels = labels

        sec_cw = _rearrange_cw(sections, sections_cw_tensor)
        cw = _rearrange_cw(labels, labels_cw_tensor)
        return input_ids, (sections, labels), (sec_cw, cw)

    return map_fn


def run_train(strategy, config, train_dataset, valid_dataset, steps_per_epoch):
    # Build CoRT models
    if config.include_sections:
        model = CortForElaboratedRepresentation(config)
    else:
        model = CortModel(config)

    if config.restore_checkpoint:
        model.load_weights(config.restore_checkpoint)
    accumulator = GradientAccumulator()
    total_train_steps = config.epochs * steps_per_epoch
    optimizer, learning_rate_fn = create_optimizer(config, total_train_steps)
    num_steps = 0

    # Metrics
    def create_metric_map():
        metric_map = dict()
        metric_map['total_loss'] = metrics.Mean(name='total_loss')
        metric_map['contrastive_loss'] = metrics.Mean(name='contrastive_loss')
        metric_map['cross_entropy_loss'] = metrics.Mean(name='cross_entropy_loss')
        metric_map['accuracy'] = metrics.CategoricalAccuracy(name='accuracy')
        metric_map['precision'] = metrics.Precision(name='precision')
        metric_map['recall'] = metrics.Recall(name='recall')
        metric_map['micro_f1_score'] = metrics_tfa.F1Score(
            num_classes=config.num_labels, average='micro', name='micro_f1_score'
        )
        metric_map['macro_f1_score'] = metrics_tfa.F1Score(
            num_classes=config.num_labels, average='macro', name='macro_f1_score'
        )

        # metrics for CoRT with elaborated representation
        if config.include_sections:
            metric_map['section_contrastive_loss'] = metrics.Mean(name='section_contrastive_loss')
            metric_map['section_cross_entropy_loss'] = metrics.Mean(name='section_cross_entropy_loss')
            metric_map['section_accuracy'] = metrics.CategoricalAccuracy(name='section_accuracy')
            metric_map['section_precision'] = metrics.Precision(name='section_precision')
            metric_map['section_recall'] = metrics.Recall(name='section_recall')
            metric_map['section_micro_f1_score'] = metrics_tfa.F1Score(
                num_classes=config.num_sections, average='micro', name='section_micro_f1_score'
            )
            metric_map['section_macro_f1_score'] = metrics_tfa.F1Score(
                num_classes=config.num_sections, average='macro', name='section_macro_f1_score'
            )
        return metric_map

    def metric_fn(dicts, model_outputs):
        d = model_outputs
        dicts['contrastive_loss'].update_state(d['contrastive_loss'])
        dicts['cross_entropy_loss'].update_state(d['cross_entropy_loss'])
        confusion_keys = ['accuracy', 'precision', 'recall',
                          'micro_f1_score', 'macro_f1_score']
        for key in confusion_keys:
            dicts[key].update_state(
                y_true=d['ohe_labels'],
                y_pred=d['probs']
            )
        if config.include_sections:
            # metrics for CoRT with elaborated representation
            dicts['section_contrastive_loss'].update_state(d['section_contrastive_loss'])
            dicts['section_cross_entropy_loss'].update_state(d['section_cross_entropy_loss'])
            confusion_keys = ['section_' + key for key in confusion_keys]
            for key in confusion_keys:
                dicts[key].update_state(
                    y_true=d['section_ohe_labels'],
                    y_pred=d['section_probs']
                )
        return dicts

    def create_metric_logs(dicts):
        metric_logs = {}
        for k, v in dicts.items():
            value = float(v.result().numpy())
            metric_logs[k] = value
        return metric_logs

    metric_maps = {
        'train': create_metric_map(),
        'valid': create_metric_map()
    }

    ckpt_file_name = 'CoRT_sweep-{}_run-{}_epoch-{}/'.format(wandb.run.sweep_id, wandb.run.id, '{epoch:02d}')
    # Callbacks
    callback_list = [
        wandb.keras.WandbModelCheckpoint(os.path.join('./models', ckpt_file_name),
                                         monitor='val_total_loss',
                                         verbose=1, save_best_only=True, save_weights_only=True),
        wandb.keras.WandbCallback()
    ]
    callback = callbacks.CallbackList(callbacks=callback_list,
                                      model=model,
                                      epochs=config.epochs,
                                      steps=steps_per_epoch)

    # Training
    def strategy_reduce_mean(unscaled_loss, outputs):
        features = dict()
        for key, value in outputs.items():
            features[key] = strategy.reduce(tf.distribute.ReduceOp.MEAN, value, axis=None)
        unscaled_loss = strategy.reduce(tf.distribute.ReduceOp.MEAN, unscaled_loss, axis=None)
        return unscaled_loss, features

    def wrap_inputs(inputs):
        if config.include_sections:
            return inputs

        if len(inputs) == 3:
            input_ids, (_, labels), (_, cw) = inputs
            return input_ids, labels, cw
        elif len(inputs) == 2:
            input_ids, (_, labels) = inputs
            return input_ids, labels
        else:
            raise ValueError('Number of inputs must be 2 or 3. Received {} instead'.format(len(inputs)))

    @tf.function
    def train_step(inputs, take_step):
        with tf.GradientTape() as tape:
            total_loss, outputs = model(wrap_inputs(inputs), training=True)
            unscaled_loss = tf.stop_gradient(total_loss)
        grads = tape.gradient(total_loss, model.trainable_variables)

        # Accumulate gradients
        accumulator(grads)
        if take_step:
            # All reduce and clip the accumulated gradients
            reduced_accumulated_gradients = [
                None if g is None else g / tf.cast(config.gradient_accumulation_steps, g.dtype)
                for g in accumulator.accumulated_gradients
            ]
            (clipped_accumulated_gradients, _) = tf.clip_by_global_norm(reduced_accumulated_gradients, clip_norm=1.0)

            # Weight update
            optimizer.apply_gradients(zip(clipped_accumulated_gradients, model.trainable_variables))
            accumulator.reset()

        return unscaled_loss, outputs

    @tf.function
    def distributed_train_step(inputs, take_step):
        unscaled_loss, outputs = strategy.run(train_step, args=(inputs, take_step))
        return strategy_reduce_mean(unscaled_loss, outputs)

    @tf.function
    def test_step(inputs):
        return model(wrap_inputs(inputs), training=False)

    @tf.function
    def distributed_test_step(inputs,):
        unscaled_loss, outputs = strategy.run(test_step, args=(inputs,))
        return strategy_reduce_mean(unscaled_loss, outputs)

    train_fn = distributed_train_step if config.distribute else train_step
    test_fn = distributed_test_step if config.distribute else test_step

    def evaluate(run_callback=True):
        if run_callback:
            callback.on_test_begin()
        for index, inputs in enumerate(valid_dataset):
            if run_callback:
                callback.on_test_batch_begin(index)
            total_loss, outputs = test_fn(inputs)

            # assign new metrics
            metric_maps['valid']['total_loss'].update_state(values=total_loss)
            metric_fn(metric_maps['valid'], outputs)

            if run_callback:
                callback.on_test_batch_end(index, logs=create_metric_logs(metric_maps['valid']))
        logs = create_metric_logs(metric_maps['valid'])
        if run_callback:
            callback.on_test_end(logs)

        val_logs = {'val_' + key: value for key, value in logs.items()}
        # WandB step-wise logging after evaluation
        wandb.log(val_logs, step=num_steps)
        return val_logs

    def reset_metrics():
        # reset all metric states
        for key in metric_maps.keys():
            [metric.reset_state() for metric in metric_maps[key].values()]

    if not config.skip_early_eval:
        # very first evaluate for initial metric results
        evaluate(run_callback=False)
    else:
        logging.info('Skipping early evaluation')

    training_logs = None
    callback.on_train_begin()
    for epoch in range(config.initial_epoch, config.epochs):
        reset_metrics()
        callback.on_epoch_begin(epoch)
        print('\nEpoch {}/{}'.format(epoch + 1, config.epochs))

        progbar = utils.Progbar(steps_per_epoch,
                                stateful_metrics=[metric.name for metric in metric_maps['train'].values()])
        accumulator.reset()
        local_step = 0
        for step, input_batches in enumerate(train_dataset.take(steps_per_epoch * config.gradient_accumulation_steps)):
            # Need to call apply_gradients on very first step irrespective of gradient accumulation
            # This is required for the optimizer to build its states
            accumulation_step = (step + 1) % config.gradient_accumulation_steps == 0 or num_steps == 0
            if accumulation_step:
                callback.on_train_batch_begin(local_step)

            training_loss, eval_inputs = train_fn(
                input_batches, take_step=accumulation_step
            )

            if accumulation_step:
                # assign new metrics
                metric_maps['train']['total_loss'].update_state(values=training_loss)
                metric_fn(metric_maps['train'], eval_inputs)

                batch_logs = create_metric_logs(metric_maps['train'])
                progbar.update(local_step, values=[(k, v) for k, v in batch_logs.items()])

                # WandB step-wise logging during training
                wandb.log(batch_logs, step=num_steps)
                wandb.log({
                    'learning_rate': learning_rate_fn(num_steps)
                }, step=num_steps)

                callback.on_train_batch_end(local_step, logs=batch_logs)
                local_step += 1
                num_steps += 1

        train_logs = create_metric_logs(metric_maps['train'])
        epoch_logs = copy.copy(train_logs)

        eval_logs = evaluate()
        epoch_logs.update(eval_logs)

        progbar.update(steps_per_epoch,
                       values=[(k, v) for k, v in epoch_logs.items()],
                       finalize=True)
        training_logs = epoch_logs
        callback.on_epoch_end(epoch, logs=epoch_logs)
    callback.on_train_end(training_logs)


def main():
    setup_formatter(level=logging.INFO)
    config = utils.parse_arguments()

    if config.cross_validation == 'kfold':
        wandb.init(project='CoRT', name='CoRT-KFOLD_{}'.format(config.current_fold + 1))
    elif config.cross_validation == 'hyperparams':
        wandb.init(project='CoRT')
    else:
        raise ValueError('Invalid CV strategy: {}'.format(config.cross_validation))

    logging.info('WandB setup:')
    logging.info('- Sweep ID: {}'.format(wandb.run.sweep_id))
    logging.info('- Run ID: {}'.format(wandb.run.id))

    gpus = tf.config.list_physical_devices('GPU')
    if len(gpus) == 0:
        raise ValueError('No available GPUs')

    if config.gpu != 'all':
        desired_gpu = gpus[int(config.gpu)]
        tf.config.set_visible_devices(desired_gpu, 'GPU')
        tf.config.experimental.set_memory_growth(desired_gpu, True)
        logging.info('Restricting GPU as /device:GPU:{}'.format(config.gpu))

    strategy = tf.distribute.MirroredStrategy()
    if config.distribute:
        logging.info('Distributed Training Enabled')
        config.batch_size = config.batch_size * strategy.num_replicas_in_sync

    if config.include_sections:
        logging.info('Elaborated Representation Enabled')

    if config.repr_preact:
        logging.info('Pre-Activated Representation Enabled')

    utils.set_random_seed(config.seed)

    tokenizer = utils.create_tokenizer_from_config(config)
    input_ids, labels = setup_datagen(config, tokenizer)
    folded_output = splits_into_fold(config, tokenizer, input_ids, labels)

    def data_generator(datagen):
        def _generator():
            for i in range(datagen.steps_per_epoch):
                yield datagen[i]

        return _generator

    output_signature = (
        tf.TensorSpec((None, None), dtype=tf.int32, name='input_ids'),
        (
            tf.TensorSpec((None,), dtype=tf.int32, name='sections'),
            tf.TensorSpec((None,), dtype=tf.int32, name='labels')
        )
    )
    if config.dynamic_datagen:
        train_dataset = tf.data.Dataset.from_generator(
            generator=data_generator(folded_output.training),
            output_signature=output_signature
        )
    else:
        train_dataset = tf.data.Dataset.from_tensor_slices(folded_output.training)

    train_dataset = train_dataset.map(class_weight_map_fn(folded_output.sections_cw, folded_output.labels_cw))
    train_dataset = train_dataset.prefetch(buffer_size=tf.data.AUTOTUNE)
    if not config.dynamic_datagen:
        train_dataset = train_dataset.batch(config.batch_size)
    train_dataset = train_dataset.shuffle(buffer_size=1024).repeat()

    if config.dynamic_datagen:
        valid_dataset = tf.data.Dataset.from_generator(
            generator=data_generator(folded_output.validation),
            output_signature=output_signature
        )
    else:
        valid_dataset = tf.data.Dataset.from_tensor_slices(folded_output.validation)
        valid_dataset = valid_dataset.batch(config.batch_size)

    if config.distribute:
        options = tf.data.Options()
        options.experimental_distribute.auto_shard_policy = tf.data.experimental.AutoShardPolicy.DATA

        train_dataset = strategy.experimental_distribute_dataset(train_dataset.with_options(options))
        valid_dataset = strategy.experimental_distribute_dataset(valid_dataset.with_options(options))

    with strategy.scope() if config.distribute else utils.empty_context_manager():
        run_train(strategy, config, train_dataset, valid_dataset, folded_output.steps_per_epoch)


if __name__ == '__main__':
    main()
