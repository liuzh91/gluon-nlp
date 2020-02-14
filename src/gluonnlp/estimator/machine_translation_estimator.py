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
""" Gluon Machine Translation Estimator """

from mxnet.gluon.contrib.estimator import Estimator
from .machine_translation_batch_processor import MTTransformerBatchProcessor

__all__ = ['MachineTranslationEstimator']

class MachineTranslationEstimator(Estimator):
    '''Estimator class for machine translation tasks

    Facilitates training and validation on machine translation tasks
    Parameters
    ----------
    net : gluon.Block
        The model used for training.
    loss : gluon.loss.Loss
        Loss (objective) function to calculate during training.
    train_metrics : EvalMetric or list of EvalMetric
        Training metrics for evaluating models on training dataset.
    val_metrics : EvalMetric or list of EvalMetric
        Validation metrics for evaluating models on validation dataset.
    initializer : Initializer
        Initializer to initialize the network.
    trainer : Trainer
        Trainer to apply optimizer on network parameters.
    context : Context or list of Context
        Device(s) to run the training on.
    val_net : gluon.Block
        The model used for validation. The validation model does not necessarily belong to
        the same model class as the training model.
    val_loss : gluon.loss.loss
        Loss (objective) function to calculate during validation. If set val_loss
        None, it will use the same loss function as self.loss
    batch_processor: BatchProcessor
        BatchProcessor provides customized fit_batch() and evaluate_batch() methods
    '''
    def __init__(self, net, loss,
                 train_metrics=None,
                 val_metrics=None,
                 initializer=None,
                 trainer=None,
                 context=None,
                 val_loss=None,
                 val_net=None,
                 batch_processor=MTTransformerBatchProcessor()):
        super().__init__(net=net, loss=loss,
                         train_metrics=train_metrics,
                         val_metrics=val_metrics,
                         initializer=initializer,
                         trainer=trainer,
                         context=context,
                         val_loss=val_loss,
                         val_net=val_net,
                         batch_processor=batch_processor)
        self.tgt_valid_length = 0
        self.val_tgt_valid_length = 0
        self.avg_param = None
        self.bleu_score = 0.0