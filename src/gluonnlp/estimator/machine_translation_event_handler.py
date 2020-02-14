# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=eval-used, redefined-outer-name
""" Gluon Machine Translation Event Handler """

import math
import os
import time

import numpy as np
import mxnet as mx
from mxnet.gluon.contrib.estimator import TrainBegin, TrainEnd, EpochBegin
from mxnet.gluon.contrib.estimator import EpochEnd, BatchBegin, BatchEnd
from mxnet.gluon.contrib.estimator import GradientUpdateHandler, CheckpointHandler
from mxnet.gluon.contrib.estimator import MetricHandler, LoggingHandler
from mxnet import gluon
from mxnet.metric import Loss as MetricLoss
from ..metric.length_normalized_loss import LengthNormalizedLoss

__all__ = ['MTTransformerParamUpdateHandler', 'TransformerLearningRateHandler',
           'MTTransformerMetricHandler', 'TransformerGradientAccumulationHandler',
           'ComputeBleuHandler', 'ValBleuHandler', 'MTGNMTGradientUpdateHandler',
           'MTGNMTLearningRateHandler', 'MTCheckpointHandler',
           'MTTransformerLoggingHandler']

class MTTransformerParamUpdateHandler(EpochBegin, BatchEnd, EpochEnd):
    '''Transformer average parameter update handler

    Update weighted average parameters of the transformer during training

    Parameters
    ----------
    avg_start : int
        the starting epoch of performing average sgd update
    grad_interval : int
        The interval of update avarege model parameters
    '''
    def __init__(self, avg_start, grad_interval=1):
        self.batch_id = 0
        self.grad_interval = grad_interval
        self.step_num = 0
        self.avg_start = avg_start

    def _update_avg_param(self, estimator):
        if estimator.avg_param is None:
            estimator.avg_param = {k:v.data(estimator.context[0]).copy() for k, v in
                                   estimator.net.collect_params().items()}
        if self.step_num > self.avg_start:
            params = estimator.net.collect_params()
            alpha = 1. / max(1, self.step_num - self.avg_start)
            for key, val in estimator.avg_param.items():
                val[:] += alpha * (params[key].data(estimator.context[0]) - val)

    def epoch_begin(self, estimator, *args, **kwargs):
        self.batch_id = 0

    def batch_end(self, estimator, *args, **kwargs):
        if self.batch_id % self.grad_interval == 0:
            self.step_num += 1
        if self.batch_id % self.grad_interval == self.grad_interval - 1:
            self._update_avg_param(estimator)
        self.batch_id += 1

    def epoch_end(self, estimator, *args, **kwargs):
        self._update_avg_param(estimator)

class MTGNMTLearningRateHandler(EpochEnd):
    '''GNMT learning rate update handler

    dynamically adjust the learning rate during GNMT training

    Parameters
    ----------
    epochs : int
        total number of epoches for GNMT training
    lr_update_factor : float
        the decaying factor of learning rate
    '''
    def __init__(self, epochs, lr_update_factor):
        self.epoch_id = 0
        self.epochs = epochs
        self.lr_update_factor = lr_update_factor

    def epoch_end(self, estimator, *args, **kwargs):
        if self.epoch_id + 1 >= (self.epochs * 2) // 3:
            new_lr = estimator.trainer.learning_rate * self.lr_update_factor
            estimator.trainer.set_learning_rate(new_lr)
        self.epoch_id += 1

class TransformerLearningRateHandler(EpochBegin, BatchBegin):
    '''Transformer learning rate update handler

    dynamically adjust the learning rate during transformer training

    Parameters
    ----------
    lr : float
        initial learning rate for transformer training
    num_units : int
        dimension of the embedding vector
    warmup_steps : int
        number of warmup steps used in training schedule
    grad_interval : int
        the interval of updating learning rate
    '''
    def __init__(self, lr,
                 num_units=512,
                 warmup_steps=4000,
                 grad_interval=1):
        self.lr = lr
        self.num_units = num_units
        self.warmup_steps = warmup_steps
        self.grad_interval = grad_interval
        self.step_num = 0

    def epoch_begin(self, estimator, *args, **kwargs):
        self.batch_id = 0

    def batch_begin(self, estimator, *args, **kwargs):
        if self.batch_id % self.grad_interval == 0:
            self.step_num += 1
            new_lr = self.lr /  math.sqrt(self.num_units) * \
                     min(1. / math.sqrt(self.step_num), self.step_num *
                         self.warmup_steps ** (-1.5))
            estimator.trainer.set_learning_rate(new_lr)
        self.batch_id += 1

class MTGNMTGradientUpdateHandler(GradientUpdateHandler):
    '''Gradient update handler of GNMT training

    clip gradient if gradient norm exceeds some threshold during GNMT training

    Parameters
    ----------
    clip : float
        gradient norm threshold. If gradient norm exceeds this value, it should be
        scaled down to the valid range.
    '''
    def __init__(self, clip):
        super(MTGNMTGradientUpdateHandler, self).__init__()
        self.clip = clip

    def batch_end(self, estimator, *args, **kwargs):
        grads = [p.grad(estimator.context[0])
                 for p in estimator.net.collect_params().values()]
        gluon.utils.clip_global_norm(grads, self.clip)
        estimator.trainer.step(1)

class TransformerGradientAccumulationHandler(GradientUpdateHandler,
                                             TrainBegin,
                                             EpochBegin,
                                             EpochEnd):
    '''Gradient accumulation handler for transformer training

    Accumulates gradients of the network for a few iterations, and updates 
    network parameters with the accumulated gradients

    Parameters
    ----------
    grad_interval : int
        the interval of updating gradients
    batch_size : int
        number of tokens per gpu in a minibatch
    rescale_loss : float
        normalization constant
    '''
    def __init__(self, grad_interval=1,
                 batch_size=1024,
                 rescale_loss=100):
        super(TransformerGradientAccumulationHandler, self).__init__()
        self.grad_interval = grad_interval
        self.batch_size = batch_size
        self.rescale_loss = rescale_loss

    def _update_gradient(self, estimator):
        estimator.trainer.step(float(self.loss_denom) /
                               self.batch_size /self.rescale_loss)
        params = estimator.net.collect_params()
        params.zero_grad()
        self.loss_denom = 0

    def train_begin(self, estimator, *args, **kwargs):
        params = estimator.net.collect_params()
        params.setattr('grad_req', 'add')
        params.zero_grad()

    def epoch_begin(self, estimator, *args, **kwargs):
        self.batch_id = 0
        self.loss_denom = 0

    def batch_end(self, estimator, *args, **kwargs):
        self.loss_denom += estimator.tgt_valid_length
        if self.batch_id % self.grad_interval == self.grad_interval - 1:
            self._update_gradient(estimator)
        self.batch_id += 1

    def epoch_end(self, estimator, *args, **kwargs):
        if self.loss_denom > 0:
            self._update_gradient(estimator)

class MTTransformerMetricHandler(MetricHandler, BatchBegin):
    '''Metric update handler for transformer training

    Reset the local metric stats for every few iterations and include the LengthNormalizedLoss
    for metrics update
    TODO : Refactor this event handler and share it with other estimators

    Parameters
    ----------
    grad_interval : int
        interval of resetting local metrics during transformer training
    '''
    def __init__(self, *args, grad_interval=None, **kwargs):
        super(MTTransformerMetricHandler, self).__init__(*args, **kwargs)
        self.grad_interval = grad_interval

    def epoch_begin(self, estimator, *args, **kwargs):
        self.batch_id = 0
        for metric in self.metrics:
            metric.reset()

    def batch_begin(self, estimator, *args, **kwargs):
        if self.grad_interval is not None and self.batch_id % self.grad_interval == 0:
            for metric in self.metrics:
                metric.reset_local()
        self.batch_id += 1

    def batch_end(self, estimator, *args, **kwargs):
        pred = kwargs['pred']
        label = kwargs['label']
        loss = kwargs['loss']
        for metric in self.metrics:
            if isinstance(metric, MetricLoss):
                metric.update(0, loss)
            elif isinstance(metric, LengthNormalizedLoss):
                metric.update(label, loss)
            else:
                metric.update(label, pred)

class MTCheckpointHandler(CheckpointHandler, TrainEnd):
    '''Checkpoint handler for machine translation tasks training

    save model parameter checkpoint and average parameter checkpoint during transformer
    or GNMT training

    Parameters
    ----------
    average_checkpoint : bool
        whether store the average parameters of last few iterations
    num_averages : int
        number of last few model checkpoints to be averaged
    average_start : int
        performing average sgd on last average_start epochs
    epochs : int
        total epochs of machine translation model training
    '''
    def __init__(self, *args,
                 average_checkpoint=None,
                 num_averages=None,
                 average_start=0,
                 epochs=0,
                 **kwargs):
        super(MTCheckpointHandler, self).__init__(*args, **kwargs)
        self.bleu_score = 0.
        self.average_checkpoint = average_checkpoint
        self.num_averages = num_averages
        self.average_start = average_start
        self.epochs = epochs

    def epoch_end(self, estimator, *args, **kwargs):
        if estimator.bleu_score > self.bleu_score:
            self.bleu_score = estimator.bleu_score
            save_path = os.path.join(self.model_dir, 'valid_best.params')
            estimator.net.save_parameters(save_path)
        save_path = os.path.join(self.model_dir, 'epoch{:d}.params'.format(self.current_epoch))
        estimator.net.save_parameters(save_path)
        self.current_epoch += 1

    def train_end(self, estimator, *args, **kwargs):
        ctx = estimator.context
        if estimator.avg_param is not None:
            save_path = os.path.join(self.model_dir, 'average.params')
            mx.nd.save(save_path, estimator.avg_param)
        if self.average_checkpoint:
            for j in range(args.num_averages):
                params = mx.nd.load(os.path.join(self.model_dir,
                                                 'epoch{:d}.params'.format(self.epochs - j - 1)))
                alpha = 1. / (j + 1)
                for k, v in estimator.net._collect_params_with_prefix().items():
                    for c in ctx:
                        v.data(c)[:] = alpha * (params[k].as_in_context(c) - v.data(c))
            save_path = os.path.join(self.model_dir,
                                     'average_checkpoint_{}.params'.format(self.num_averages))
            estimator.net.save_parameters(save_path)
        elif self.average_start:
            for k, v in estimator.net.collect_params().items():
                v.set_data(estimator.avg_param[k])
            save_path = os.path.join(self.model_dir, 'average.params')
            estimator.net.save_parameters(save_path)
        else:
            estimator.net.load_parameters(os.path.join(self.model_dir,
                                                       'valid_best.params'), ctx)


class ComputeBleuHandler(BatchEnd, EpochEnd):
    '''Bleu score computation handler

    this event handler serves as a temporary workaround for computing Bleu score for
    estimator training.
    TODO: please remove this event handler after bleu metrics is merged to api
    '''
    def __init__(self,
                 tgt_vocab,
                 tgt_sentence,
                 translator,
                 compute_bleu_fn,
                 tokenized=True,
                 tokenizer='13a',
                 split_compound_word=False,
                 bpe=False,
                 bleu='13a',
                 detokenizer=None,
                 _bpe_to_words=None):
        self.tgt_vocab = tgt_vocab
        self.tgt_sentence = tgt_sentence
        self.translator = translator
        self.compute_bleu_fn = compute_bleu_fn
        self.tokenized = tokenized
        self.tokenizer = tokenizer
        self.split_compound_word = split_compound_word
        self.bpe = bpe
        self.bleu = bleu
        self.detokenizer = detokenizer
        self._bpe_to_words = _bpe_to_words

        self.all_inst_ids = []
        self.translation_out = []

    def batch_end(self, estimator, *args, **kwargs):
        ctx = estimator.context[0]
        batch = kwargs['batch']
        src_seq, tgt_seq, src_valid_length, tgt_valid_length, inst_ids = batch
        src_seq = src_seq.as_in_context(ctx)
        tgt_seq = tgt_seq.as_in_context(ctx)
        src_valid_length = src_valid_length.as_in_context(ctx)
        tgt_valid_length = tgt_valid_length.as_in_context(ctx)
        self.all_inst_ids.extend(inst_ids.asnumpy().astype(np.int32).tolist())
        samples, _, sample_valid_length = self.translator.translate(
            src_seq=src_seq, src_valid_length=src_valid_length)
        max_score_sample = samples[:, 0, :].asnumpy()
        sample_valid_length = sample_valid_length[:, 0].asnumpy()
        for i in range(max_score_sample.shape[0]):
            self.translation_out.append(
                [self.tgt_vocab.idx_to_token[ele] for ele in
                 max_score_sample[i][1:(sample_valid_length[i] - 1)]])

    def epoch_end(self, estimator, *args, **kwargs):
        real_translation_out = [None for _ in range(len(self.all_inst_ids))]
        for ind, sentence in zip(self.all_inst_ids, self.translation_out):
            if self.bleu == 'tweaked':
                real_translation_out[ind] = sentence
            elif self.bleu == '13a' or self.bleu == 'intl':
                real_translation_out[ind] = self.detokenizer(self._bpe_to_words(sentence))
            else:
                raise NotImplementedError
        estimator.bleu_score, _, _, _, _ = \
        self.compute_bleu_fn([self.tgt_sentence],
                             real_translation_out,
                             tokenized=self.tokenized,
                             tokenizer=self.tokenizer,
                             split_compound_word=self.split_compound_word,
                             bpe=self.bpe)
        print(estimator.bleu_score)

class ValBleuHandler(EpochEnd):
    '''Handler of validation Bleu score computation

    This handler is similar to the ComputeBleuHandler. It computes the Bleu score on the 
    validation dataset
    TODO: please remove this event handler after bleu metric is available in the api
    '''
    def __init__(self, val_data,
                 val_tgt_vocab,
                 val_tgt_sentences,
                 translator,
                 compute_bleu_fn,
                 tokenized=True,
                 tokenizer='13a',
                 split_compound_word=False,
                 bpe=False,
                 bleu='13a',
                 detokenizer=None,
                 _bpe_to_words=None):
        self.val_data = val_data
        self.val_tgt_vocab = val_tgt_vocab
        self.val_tgt_sentences = val_tgt_sentences
        self.translator = translator
        self.tokenized = tokenized
        self.tokenizer = tokenizer
        self.split_compound_word = split_compound_word
        self.bpe = bpe
        self.compute_bleu_fn = compute_bleu_fn
        self.bleu = bleu
        self.detokenizer = detokenizer
        self._bpe_to_words = _bpe_to_words

    def epoch_end(self, estimator, *args, **kwargs):
        translation_out = []
        all_inst_ids = []
        for  _, (src_seq, tgt_seq, src_valid_length, tgt_valid_length, inst_ids) \
             in enumerate(self.val_data):
            src_seq = src_seq.as_in_context(estimator.context[0])
            tgt_seq = tgt_seq.as_in_context(estimator.context[0])
            src_valid_length = src_valid_length.as_in_context(estimator.context[0])
            tgt_valid_length = tgt_valid_length.as_in_context(estimator.context[0])
            all_inst_ids.extend(inst_ids.asnumpy().astype(np.int32).tolist())
            samples, _, sample_valid_length = self.translator.translate(
                src_seq=src_seq, src_valid_length=src_valid_length)
            max_score_sample = samples[:, 0, :].asnumpy()
            sample_valid_length = sample_valid_length[:, 0].asnumpy()
            for i in range(max_score_sample.shape[0]):
                translation_out.append(
                    [self.val_tgt_vocab.idx_to_token[ele] for ele in
                     max_score_sample[i][1:(sample_valid_length[i] - 1)]])
        real_translation_out = [None for _ in range(len(all_inst_ids))]
        for ind, sentence in zip(all_inst_ids, translation_out):
            if self.bleu == 'tweaked':
                real_translation_out[ind] = sentence
            elif self.bleu == '13a' or self.bleu == 'intl':
                real_translation_out[ind] = self.detokenizer(self._bpe_to_words(sentence))
            else:
                raise NotImplementedError
        estimator.bleu_score, _, _, _, _ = \
        self.compute_bleu_fn([self.val_tgt_sentences],
                             real_translation_out,
                             tokenized=self.tokenized,
                             tokenizer=self.tokenizer,
                             split_compound_word=self.split_compound_word,
                             bpe=self.bpe)
        print(estimator.bleu_score)

class MTTransformerLoggingHandler(LoggingHandler):
    '''Logging handler for transformer training

    Logging the training metrics for transformer training. This handler is introduced 
    due to batch cannot be handled by default LoggingHandler 
    '''
    def __init__(self, *args, **kwargs):
        super(MTTransformerLoggingHandler, self).__init__(*args, **kwargs)

    def batch_end(self, estimator, *args, **kwargs):
        if isinstance(self.log_interval, int):
            batch_time = time.time() - self.batch_start
            msg = '[Epoch %d][Batch %d]' % (self.current_epoch, self.batch_index)
            cur_batches = kwargs['batch']
            for batch in cur_batches:
                self.processed_samples += batch[0].shape[0]
            msg += '[Samples %s]' % (self.processed_samples)
            self.log_interval_time += batch_time
            if self.batch_index % self.log_interval == 0:
                msg += 'time/interval: %.3fs ' % self.log_interval_time
                self.log_interval_time = 0
                for metric in self.metrics:
                    name, val = metric.get()
                    msg += '%s: %.4f, ' % (name, val)
                estimator.logger.info(msg.rstrip(', '))
        self.batch_index += 1