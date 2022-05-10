import os
import functools
import itertools
import traceback
from operator import itemgetter
import pandas as pd
import numpy as np
import tensorflow
import random
from matplotlib.colors import ListedColormap
import networkx as nx

from scipy.spatial import distance_matrix
from sklearn.decomposition import KernelPCA
from sklearn.metrics import f1_score, accuracy_score, confusion_matrix
from sklearn.preprocessing import MinMaxScaler

from tqdm import tqdm

import seaborn as sns
import matplotlib.pyplot as plt

#from dsprofiling.src.clustering_tools import break_down_clusters

plt.ioff()
import mplcursors

import torch

def l2_norm(input):
    buffer = input**2
    normp = np.sqrt(np.sum(buffer, axis=1))
    output = input/normp[:, None]
    return output

def calc_recall_at_k(T, Y, k):
    """
    T : [nb_samples] (target labels)
    Y : [nb_samples x k] (k predicted labels/neighbours)
    """

    s = 0
    for t,y in zip(T,Y):
        if t == torch.mode(torch.Tensor(y).long()[:k]).values:
            s += 1
    return s / (1. * len(T))

def combine_dims(a, i=0, n=1):
  """
  Combines dimensions of numpy array `a`,
  starting at index `i`,
  and combining `n` dimensions
  """
  s = list(a.shape)
  combined = functools.reduce(lambda x,y: x*y, s[i:i+n+1])
  return np.reshape(a, s[:i] + [combined] + s[i+n+1:])


def predict_batchwise(model, train_gen, return_images=False):
    batch_sz = train_gen.batch_size
    num_batches = train_gen.__len__()

    predictions = np.zeros((num_batches, batch_sz, train_gen.sz_embedding))
    labels = np.zeros((num_batches, batch_sz))

    if return_images:
        # TODO add a attribute to dataloader that gives the shape
        #image_array = np.zeros((len(dataloader.dataset.im_paths), 3, 224, 224))
        image_array = np.zeros((num_batches, batch_sz, *train_gen.im_dimensions))

    missing_batch = 0

    # extract batches (A becomes list of samples)
    for idx in range(num_batches):
        batch = train_gen.__getitem__(idx)
        for i, J in enumerate(batch):
            # i = 0: sz_batch * images
            # i = 1: sz_batch * labels
            # i = 2: sz_batch * indices
            if len(J) != batch_sz:
                filled_batch = np.array(J, dtype=np.float32)
                empty_batch = np.zeros((batch_sz - len(J), *filled_batch.shape[1::]), dtype=np.float32)
                missing_batch = len(empty_batch)

                if i == 1:
                    J = np.hstack((filled_batch, empty_batch))
                else:
                    J = np.vstack((filled_batch, empty_batch))

            if i == 0:
                if return_images:
                    image_array[i, :] = J

                # move images to device of model (approximate device)
                J = model.predict([J, np.zeros_like(batch[1])]) # Second arg is a dummy input
                                                                # because its not in training mode
                predictions[idx, :] = J
            else:
                labels[idx, :] = np.argmax(J, axis=1)

    if missing_batch == 0: missing_batch = -1 * num_batches * batch_sz

    if return_images:
        image_array = combine_dims(image_array, 0, 1)
        image_array = image_array[0:-missing_batch, :]

    predictions = combine_dims(predictions, 0, 1)
    predictions = predictions[0:-missing_batch, :]

    labels = combine_dims(labels, 0, 1)
    labels = labels[0:-missing_batch]

    if return_images:
        return [predictions, labels, image_array]
    return [predictions, labels]


def proxy_init_calc(model, dataloader):
    nb_classes = len(dataloader.classes)
    X, T, *_ = predict_batchwise(model, dataloader)

    proxy_mean = torch.stack([X[T==class_idx].mean(0) for class_idx in range(nb_classes)])

    return proxy_mean


def parse_im_name(specific_species, exclude_trailing_consonants=False, fine=False):
    if fine:
        filter = os.path.split(os.path.split(specific_species)[0])[1].split('.')[-1].lower()
    else:
        coarse_filter = os.path.split(os.path.split(specific_species)[0])[1].split('_')[-1].lower()
        if '.' in coarse_filter:
            coarse_filter = coarse_filter.split('.')[-1]
        filter = coarse_filter

    if exclude_trailing_consonants:
        if filter[-1].isalpha():
            filter = filter[0:-1]
    return filter


def evaluate_cos(model, dataloader, epoch, args):
    # calculate embeddings with model and get targets
    dest = f'../training/{args.dataset}/{epoch}/'
    os.makedirs(dest, exist_ok=True)

    if epoch % 2 == 0:
        X, T, I = predict_batchwise(model, dataloader, return_images=False)
    else:
        X, T = predict_batchwise(model, dataloader, return_images=False)

    X = l2_norm(X)

    # get predictions by assigning nearest 8 neighbors with cosine
    K = 32
    cos_sim = tensorflow.matmul(X, X, transpose_b=True)
    neighbors = tensorflow.math.top_k(cos_sim, 1 + K)[1][:, 1:]
    Y = T[neighbors]
    recall = {}

    if epoch % 2 == 0:
        coarse_filter_dict, fine_filter_dict, metrics = get_accuracies(T, X, dataloader, neighbors)
        recall['specific_accuracy'] = metrics['specific_accuracy'].values[0]
        recall['coarse_accuracy'] = metrics['coarse_accuracy'].values[0]
    #plot_feature_space(X, dataloader)

    metrics = pd.DataFrame()
    for k in [3, 5, 7]:
        y_preds, y_true = calc_recall(T, Y, epoch, k, metrics, recall)

    if epoch % 2 == 0:
        data_viz_frame = form_data_viz_frame(X, coarse_filter_dict, dataloader, fine_filter_dict, y_preds, y_true)

        params = ['prediction', 'truth']
        degrees = ['fine', 'coarse']
        for deg in degrees:
            for para in params:
                centroids = plot_node_graph(X, data_viz_frame, dataloader, para, deg, dest)

        plot_confusion(data_viz_frame, dataloader, dest)

        metrics.to_csv(dest + 'metrics.csv')
    return recall


def calc_recall(T, Y, epoch, k, metrics, recall):
    y_preds = []
    for t, y in zip(T, Y):
        y_preds.append(torch.mode(torch.Tensor(y).long()[:k]).values)
    y_preds = np.array(y_preds).astype(int)
    y_true = np.array(T)
    r_at_k = f1_score(y_true, y_preds, average='weighted')
    recall[f"f1score@{k}"] = r_at_k
    print("f1score@{} : {:.3f}".format(k, 100 * r_at_k))
    if epoch % 2 == 0:
        metrics[f'f1score@{k}'] = r_at_k * 100
    return y_preds, y_true


def plot_confusion(data_viz_frame, dataloader, dest):
    params = ['label_coarse', 'label_fine', 'denom']
    if 'NoteStyles' != dataloader.name:
        params = ['label_coarse', 'label_fine']
    for param in params:
        fig = plt.Figure(figsize=(48, 48))
        ax = plt.subplot()
        annot = True
        if param == 'label_fine':
            annot = False
        sns.heatmap(confusion_matrix(data_viz_frame[f'prediction_{param}'].values,
                                     data_viz_frame[f'truth_{param}'].values),
                    annot=annot, fmt='g', ax=ax,
                    annot_kws={"size": 4})  # annot=True to annotate cells, ftm='g' to disable scientific notation

        # labels, title and ticks
        ax.set_xlabel('Predicted labels')
        ax.set_ylabel('True labels')
        ax.set_title(f'Confusion Matrix for {param}')
        ax.xaxis.set_ticklabels(np.unique(data_viz_frame[f'prediction_{param}'].values))
        ax.yaxis.set_ticklabels(np.unique(data_viz_frame[f'truth_{param}'].values))
        plt.savefig(dest + f'Confusion_{param}.png', bbox_inches='tight', dpi=300)
        plt.close()


def form_data_viz_frame(X, coarse_filter_dict, dataloader, fine_filter_dict, y_preds, y_true):
    data_viz_frame = pd.DataFrame(y_true.astype(int), columns=['truth'])
    data_viz_frame['prediction'] = y_preds
    data_viz_frame['truth_label_coarse'] = data_viz_frame['truth'].map(coarse_filter_dict, y_true)
    data_viz_frame['prediction_label_coarse'] = data_viz_frame['prediction'].map(coarse_filter_dict, y_preds)
    data_viz_frame['truth_label_fine'] = data_viz_frame['truth'].map(fine_filter_dict, y_true)
    data_viz_frame['prediction_label_fine'] = data_viz_frame['prediction'].map(fine_filter_dict, y_preds)
    if 'NoteStyles' == dataloader.name:
        data_viz_frame['truth_denom'] = [label.split('_')[0] for label in data_viz_frame['truth_label_fine']]
        data_viz_frame['prediction_denom'] = [label.split('_')[0] for label in data_viz_frame['prediction_label_fine']]
    data_viz_frame['mean_coarse'] = X.mean(axis=1)
    return data_viz_frame


def stratified_sample_df(df, col, n_samples):
    n = min(n_samples, df[col].value_counts().min())
    df_ = df.groupby(col).apply(lambda x: x.sample(n))
    df_.index = df_.index.droplevel(0)
    return df_


def plot_node_graph(X, data_viz_frame, dataloader, para, deg, dest):
    # TODO make this works for individual points
    centroids = {}
    full_items = []
    for idx, (coarse, frame) in enumerate(data_viz_frame.groupby(f'{para}_label_{deg}')):

        # clusters, idNo = break_down_clusters(pd.DataFrame(np.array(X[frame.index]).astype(np.float64)),
        #                     f'./dsprofiling/saved_profiling_objects/{dataloader.dataset.name}/',
        #                     dataloader.dataset.name)
        #
        # for idx, internal_cluster in enumerate(clusters):
        #     centroids[coarse + '_' + str(idx + 1)] = internal_cluster.mean()

        centroids[coarse] = X[frame.index].mean(axis=0)

        for note in frame.index:
            full_items.append((coarse, *X[note]))

    centroids_af = pd.DataFrame(centroids).T
    dst_matrix = distance_matrix(centroids_af, centroids_af, p=0.1)

    sca = MinMaxScaler()
    dst_matrix = sca.fit_transform(dst_matrix)
    fig = plt.Figure(figsize=(120, 60))

    G = nx.from_numpy_matrix(dst_matrix)

    weight_dict = {}
    for (i, j) in itertools.permutations(range(len(G.nodes)), 2):
        if i not in weight_dict.keys():
            weight_dict[i] = {}
        weight_dict[i][j] = G.get_edge_data(i, j)['weight']

    G = nx.create_empty_copy(G)

    num_nbors = 1
    for i in range(len(G.nodes)):
        try:
            res = dict(sorted(weight_dict[i].items(), key=itemgetter(1))[:num_nbors])
        except Exception:
            print(traceback.format_exc())
            print(weight_dict.keys())
            print(G.nodes)
        small_edges = [key for key, val in res.items() if val < 0.6]

        for edg in small_edges:
            if i == edg:
                print(small_edges)
                print(i)
            G.add_edge(i, edg, weight=weight_dict[i][edg])

    G = nx.relabel_nodes(G, dict(zip(range(len(G.nodes())),
                                     [i for i in centroids_af.index])))

    pos = nx.spring_layout(G)

    nx.draw_networkx_labels(G, pos=pos, bbox=dict(
        boxstyle='round', ec=(0.0, 0.0, 0.0),
        alpha=0.9, fc='white', lw=1.5
    ),
                            verticalalignment='center', ax=fig.add_subplot(111))
    nx.draw(G, node_size=600, pos=pos, with_labels=False, ax=fig.add_subplot(111))
    plt.show(block=False)
    fig.savefig(f'{dest}/node_graph_{para}_{deg}.png', bbox_inches='tight', dpi=200)
    return centroids

import math
def cosine_similarity(v1,v2):
    "compute cosine similarity of v1 to v2: (v1 dot v2)/{||v1||*||v2||)"
    sumxx, sumxy, sumyy = 0, 0, 0
    for i in range(len(v1)):
        x = v1[i]; y = v2[i]
        sumxx += x*x
        sumyy += y*y
        sumxy += x*y
    return sumxy/math.sqrt(sumxx*sumyy)


def get_accuracies(T, X, dataloader, neighbors):
    pictures_to_predict = random.choices(range(len(X)), k=int(round(len(dataloader.im_paths)*50/100)))
    ground_truth = T[pictures_to_predict]

    coarse_filter_dict = {class_num: specific_species
                          for class_num, specific_species in zip(np.array(T), dataloader.class_names_coarse)}

    fine_filter_dict = {class_num : specific_species
                        for class_num, specific_species in zip(np.array(T), dataloader.class_names_fine)}

    y_preds = np.zeros(len(pictures_to_predict))

    for idx, pic in tqdm(enumerate(pictures_to_predict), total=len(pictures_to_predict), desc='Accuracy Analysis'):
        neighbors_to_pic = np.array(neighbors[pic, :][~np.in1d(neighbors[pic, :], pictures_to_predict)])

        preds, counts = np.unique(T[neighbors_to_pic], return_counts=True)
        close_preds = preds[np.argsort(counts)[-2::]]
        
        predictions = {}
        
        for close_pred in close_preds:
            neighbors_pred = [i for i in neighbors_to_pic if T[i] == close_pred]
            one = np.array(X[neighbors_pred])
            two = np.array(X[pic]).reshape(-1, 1).T

            norms_X = np.sqrt((one * one).sum(axis=1))  # Or np.linalg.norm(axis=1)
            one /= norms_X[:, np.newaxis]
            norms_Y = np.sqrt((two * two).sum(axis=1))
            two /= norms_Y[:, np.newaxis]
            cs = np.dot(one, two.T)

            predictions[close_pred] = (sum(cs)/np.sqrt(len(cs)))[0]

        y_preds[idx] = max(predictions, key=predictions.get)

    print(f'Accuracy at Specific: {accuracy_score(y_preds, np.array(ground_truth)) * 100}')

    coarse_predictions = [coarse_filter_dict[pred] for pred in y_preds]
    coarse_truth = [coarse_filter_dict[truth] for truth in np.array(ground_truth)]
    print(f'Accuracy at Coarse: {accuracy_score(coarse_predictions, coarse_truth) * 100}')

    metrics = pd.DataFrame([accuracy_score(y_preds, np.array(ground_truth)) * 100])
    metrics.columns = ['specific_accuracy']
    metrics['coarse_accuracy'] = accuracy_score(coarse_predictions, coarse_truth) * 100
    return coarse_filter_dict, fine_filter_dict, metrics

def plot_feature_space(X, dataloader):
    inspect_space = np.array(X)
    kernel_key = 'sigmoid'
    pca = KernelPCA(n_components=3, kernel=kernel_key)
    transformed_space = pca.fit_transform(inspect_space)
    coarse_filters = [parse_im_name(specific_species) for specific_species in dataloader.im_paths]
    plot_based_on_cluster_id(np.array(coarse_filters), {kernel_key: transformed_space}, kernel=kernel_key)


def plot_based_on_cluster_id(cluster_id, transformedDfs, kernel='all'):
    cmap = ListedColormap(sns.color_palette("husl", len(np.unique(cluster_id))).as_hex())
    colours = {pnt: cmap.colors[idx] for idx, pnt in enumerate(np.unique(cluster_id))}

    for kernel_key, transformedDf in transformedDfs.items():
        if kernel != 'all' and kernel != kernel_key:
            continue

        fig = plt.figure(figsize=(12, 12))
        ax = plt.axes(projection='3d')
        for pnt in np.unique(cluster_id):
            pnt_bool = [pnt == ii for ii in cluster_id]
            ax.scatter(transformedDf[pnt_bool, 0],
                       transformedDf[pnt_bool, 1],
                       transformedDf[pnt_bool, 2],
                       s=40, c=colours[pnt], marker='o', alpha=1, label=pnt)
        ax.legend()
        mplcursors.cursor(ax).connect("add", lambda sel: sel.annotation.set_text(sel.artist.get_label()))
        # save
        fig.suptitle(kernel_key)
        plt.show()

