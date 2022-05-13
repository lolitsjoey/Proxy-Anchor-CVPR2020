import argparse, os
import urllib

import utils, losses
import numpy as np
import tensorflow as tf

from tensorflow.keras import Model
from tensorflow.keras.layers import Input
from generator import NoteStyles, Cars

import tensorflow_addons as tfa
import tensorflow_hub as hub


def configure_parser():
    parser = argparse.ArgumentParser(description=
                                     'Official implementation of `Proxy Anchor Loss for Deep Metric Learning`'
                                     + 'Our code is modified from `https://github.com/dichotomies/proxy-nca`'
                                     )
    # export directory, training and val datasets, test datasets
    parser.add_argument('--LOG_DIR',
                        default='../logs',
                        help='Path to log folder'
                        )
    parser.add_argument('--dataset',
                        default='cub',
                        help='Training dataset, e.g. cub, cars, SOP, Inshop'
                        )
    parser.add_argument('--embedding-size', default=512, type=int,
                        dest='sz_embedding',
                        help='Size of embedding that is appended to backbone model.'
                        )
    parser.add_argument('--batch-size', default=150, type=int,
                        dest='sz_batch',
                        help='Number of samples per batch.'
                        )
    parser.add_argument('--epochs', default=60, type=int,
                        dest='nb_epochs',
                        help='Number of training epochs.'
                        )
    parser.add_argument('--gpu-id', default=0, type=int,
                        help='ID of GPU that is used for training.'
                        )
    parser.add_argument('--workers', default=0, type=int,
                        dest='nb_workers',
                        help='Number of workers for dataloader.'
                        )
    parser.add_argument('--model', default='bn_inception',
                        help='Model for training'
                        )
    parser.add_argument('--loss', default='Proxy_Anchor',
                        help='Criterion for training'
                        )
    parser.add_argument('--optimizer', default='adamw',
                        help='Optimizer setting'
                        )
    parser.add_argument('--lr', default=1e-4, type=float,
                        help='Learning rate setting'
                        )
    parser.add_argument('--weight-decay', default=1e-4, type=float,
                        help='Weight decay setting'
                        )
    parser.add_argument('--lr-decay-step', default=10, type=int,
                        help='Learning decay step setting'
                        )
    parser.add_argument('--lr-decay-gamma', default=0.5, type=float,
                        help='Learning decay gamma setting'
                        )
    parser.add_argument('--alpha', default=32, type=float,
                        help='Scaling Parameter setting'
                        )
    parser.add_argument('--mrg', default=0.1, type=float,
                        help='Margin parameter setting'
                        )
    parser.add_argument('--IPC', type=int,
                        help='Balanced sampling, images per class'
                        )
    parser.add_argument('--warm', default=1, type=int,
                        help='Warmup training epochs'
                        )
    parser.add_argument('--bn-freeze', default=1, type=int,
                        help='Batch normalization parameter freeze'
                        )
    parser.add_argument('--l2-norm', default=1, type=int,
                        help='L2 normlization'
                        )
    parser.add_argument('--remark', default='',
                        help='Any reamrk'
                        )
    return parser.parse_args()


def create_and_compile_model(train_gen, args):
    # model = model
    y_input = Input(shape=(1,))
    backbone = tf.keras.Sequential(hub.KerasLayer("https://tfhub.dev/google/imagenet/inception_v2/classification/5", trainable=True))

                              # arguments=dict(return_endpoints=True)))
    backbone.build([None, *train_gen.im_dimensions])
    flat = tf.keras.layers.Flatten()(backbone.output)
    embed = tf.keras.layers.Dense(args.sz_embedding, kernel_initializer=tf.keras.initializers.HeNormal(),
                                  use_bias=False, activation=None)(flat)

    criterion = losses.TF_proxy_anchor(len(set(train_gen.ys)), args.sz_embedding)([y_input, embed])

    model = Model(inputs=[backbone.input, y_input], outputs=criterion)
    optimizers = [
        tfa.optimizers.AdamW(learning_rate=float(args.lr), weight_decay=args.weight_decay),
        tfa.optimizers.AdamW(learning_rate=float(args.lr)*100, weight_decay=args.weight_decay)
    ]
    optimizers_and_layers = [(optimizers[0], model.layers[0:-1]), (optimizers[1], model.layers[-1])]
    optimizer = tfa.optimizers.MultiOptimizer(optimizers_and_layers)
    model.compile(optimizer=optimizer,
                  run_eagerly=False)
    return model, criterion


def create_generators(args, seed):
    if args.dataset == 'note_styles':
        train_gen = NoteStyles(args, seed, shuffle=True, mode='train')
        val_gen = NoteStyles(args, seed, shuffle=True, mode='val')
        test_gen = NoteStyles(args, seed, shuffle=True, mode='test')

    elif args.dataset == 'cars':
        train_gen = Cars(args, seed, shuffle=True, mode='train')
        val_gen = Cars(args, seed, shuffle=True, mode='val')
        test_gen = Cars(args, seed, shuffle=True, mode='test')
    return train_gen, val_gen, test_gen


def create_save_dir(args):
    checkpoint_filepath = args.LOG_DIR \
                          + '/logs_{}/{}_{}_embedding{}_alpha{}_mrg{}_{}_lr{}_batch{}{}'.format(args.dataset,
                                                                                                args.model,
                                                                                                args.loss,
                                                                                                args.sz_embedding,
                                                                                                args.alpha,
                                                                                                args.mrg,
                                                                                                args.optimizer,
                                                                                                args.lr, args.sz_batch,
                                                                                                args.remark)
    return checkpoint_filepath


def test_predictions(args, epoch, model, train_gen, val_gen, test_gen):
    predict_model = Model(inputs=model.input, outputs=model.layers[-2].output)

    print('###################################')
    print(f'######  TEST EPOCh {epoch}  #######')
    Recalls = utils.evaluate_cos(predict_model, test_gen, epoch, args)

    print('###################################')
    print(f'###### TRAIN EPOCh {epoch}  #######')
    Recalls = utils.evaluate_cos(predict_model, train_gen, epoch, args)

    print('####################################')
    print(f'######   VAL EPOCh {epoch}  #######')
    Recalls = utils.evaluate_cos(predict_model, val_gen, epoch, args)

def prepare_layers(args, epoch, model):
    bn_freeze = args.bn_freeze
    if bn_freeze:
        for layer in model.layers:
            if layer.name == 'batch_normalization':
                layer.trainable = False
    if args.warm > 0:
        if epoch == 0:
            model.layers[-1].trainable = False
        if epoch == args.warm:
            model.layers[-1].trainable = True


def main():
    args = configure_parser()

    os.chdir('../data/')
    data_root = os.getcwd()
    # Dataset Loader and Sampler

    seed = np.random.choice(range(144444))
    train_gen, val_gen, test_gen = create_generators(args, seed)

    save_path = create_save_dir(args)
    model_dir = save_path + './untrained_model.h5'

    try:
        model, criterion = create_and_compile_model(train_gen, args)
        tf.keras.models.save_model(model, model_dir)
    except urllib.error.URLError:
        print(f"Cant create from scratch, loading from {model_dir}")
        model = tf.keras.models.load_model(model_dir, custom_objects={'KerasLayer': hub.KerasLayer,
                                                                       'TF_proxy_anchor': losses.TF_proxy_anchor})
        model.compile(optimizer=tfa.optimizers.Adam(learning_rate=float(args.lr), weight_decay=args.weight_decay))

    print("Training for {} epochs.".format(args.nb_epochs))

    model_checkpoint_callback = tf.keras.callbacks.ModelCheckpoint(
                                                            filepath=save_path,
                                                            save_weights_only=True,
                                                            monitor='train_loss',
                                                            mode='min',
                                                            save_best_only=True
    )
    tensorBoard = tf.keras.callbacks.TensorBoard(log_dir=save_path, histogram_freq=1)

    for epoch in range(0, args.nb_epochs):
        prepare_layers(args, epoch, model)

        model.fit(train_gen, validation_data=val_gen, verbose=1, shuffle=False, callbacks=[model_checkpoint_callback,
                                                                                             tensorBoard])

        if (epoch >= 0 and (epoch % 3 == 0)) or (epoch == args.nb_epochs - 1):
            test_predictions(args, epoch, model, train_gen, val_gen, test_gen)


if __name__ == '__main__':
    main()

