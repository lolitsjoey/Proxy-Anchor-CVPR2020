import tensorflow as tf
import numpy as np
from tensorflow.python.ops import gen_array_ops


class BinaryTruePositives(tf.keras.metrics.Metric):
    def __init__(self, name='binary_true_positives', **kwargs):
        super(BinaryTruePositives, self).__init__(name=name, **kwargs)
        self.true_positives = self.add_weight(name='tp', initializer='zeros')

    def update_state(self, y_true, y_pred, sample_weight=None):
        y_true = tf.cast(y_true, tf.bool)
        y_pred = tf.cast(y_pred, tf.bool)

        values = tf.logical_and(tf.equal(y_true, True), tf.equal(y_pred, True))
        values = tf.cast(values, self.dtype)
        if sample_weight is not None:
            sample_weight = tf.cast(sample_weight, self.dtype)
            sample_weight = tf.broadcast_to(sample_weight, values.shape)
            values = tf.multiply(values, sample_weight)
        self.true_positives.assign_add(tf.reduce_sum(values))

    def result(self):
        return self.true_positives


class TF_proxy_anchor(tf.keras.layers.Layer):
    def __init__(self, nb_classes, sz_embedding):
        super(TF_proxy_anchor, self).__init__()
        self.nb_classes_not_tensor = nb_classes
        self.nb_classes = tf.cast(self.nb_classes_not_tensor, tf.int32)

        self.proxy = tf.compat.v1.get_variable(name='proxy',
                                               shape=[nb_classes, sz_embedding],
                                               initializer=tf.random_normal_initializer(),
                                               dtype=tf.float32,
                                               trainable=True)

    def get_config(self):
        config = super().get_config().copy()
        config.update({
            'nb_classes': self.nb_classes,
            'proxy': self.proxy
        })
        return config

    def call(self, inputs, **kwargs):
        self.add_loss(self.custom_loss(inputs[0], inputs[1]))
        return inputs[0]

    def get_vars(self):
        return self.proxy

    def custom_loss(self, target, embeddings):
        embeddings_l2 = tf.nn.l2_normalize(embeddings, axis=1)
        proxy_l2 = tf.nn.l2_normalize(self.proxy, axis=1)

        pos_target = target
        neg_target = 1.0 - pos_target

        sim_mat = tf.matmul(embeddings_l2, proxy_l2, transpose_b=True)

        pos_mat = tf.matmul(tf.exp(-32 * (sim_mat - 0.1)), pos_target, transpose_b=True)
        neg_mat = tf.matmul(tf.exp(32 * (sim_mat - 0.1)), neg_target, transpose_b=True)

        n_valid_proxies = tf.math.count_nonzero(tf.reduce_sum(pos_target, axis=0),
                                              dtype=tf.dtypes.float32)

        pos_term = tf.math.divide(tf.constant(1.0, dtype=tf.float32), n_valid_proxies)\
                   * tf.reduce_sum(tf.math.log(tf.constant(1.0, dtype=tf.float32) + tf.reduce_sum(pos_mat, axis=0)))

        neg_term = tf.cast(tf.constant(1) / self.nb_classes, tf.float32)\
                   * tf.reduce_sum(tf.math.log(1.0 + tf.reduce_sum(neg_mat, axis=0)))

        loss = pos_term + neg_term
        return loss
