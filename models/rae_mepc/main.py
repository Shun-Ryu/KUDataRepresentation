import os
import time
import random
import argparse
import warnings
from sklearn.model_selection import train_test_split

import torch
from torch.utils.data import Dataset, DataLoader

from model import *
from utils import *

warnings.filterwarnings("ignore")

# os.environ['CUDA_LAUNCH_BLOCKING'] = '1'


def get_data_batch(data_x, data_y=None, window_length=64, sliding_step=1):
    samples = data_x
    labels = data_y

    seq_length = window_length
    seq_step = sliding_step

    arr_samples = []
    pred_arr_samples = []
    if labels is not None:
        arr_labels = []

    arr_indexes = []
    idx = np.asarray(list(range(0, np.shape(data_x)[0])))

    s_index = 0
    e_index = s_index + seq_length

    if e_index < samples.shape[0]:
        while e_index + seq_step < samples.shape[0]:
            arr_samples.append(samples[s_index:e_index])
            pred_arr_samples.append(samples[s_index + seq_step:e_index + seq_step])
            if labels is not None:
                arr_labels.append(labels[s_index:e_index])
            arr_indexes.append(idx[s_index:e_index])
            s_index = s_index + seq_step
            e_index = e_index + seq_step

        if s_index < (samples.shape[0] - 1):
            arr_samples.append(samples[-(seq_length + seq_step):-seq_step])
            pred_arr_samples.append(samples[-seq_length:])
            if labels is not None:
                arr_labels.append(labels[-(seq_length + seq_step):-seq_step])
            arr_indexes.append(idx[-(seq_length + seq_step):-seq_step])
    else:
        arr_samples.append(samples)
        if labels is not None:
            arr_labels.append(labels)
        arr_indexes.append(idx)

    arr_samples = np.stack(arr_samples, axis=0)
    pred_arr_samples = np.stack(pred_arr_samples, axis=0)
    if labels is not None:
        arr_labels = np.stack(arr_labels, axis=0)
    arr_indexes = np.stack(arr_indexes, axis=0)

    samples = arr_samples
    if labels is not None:
        labels = arr_labels
    index = arr_indexes

    return samples, pred_arr_samples, labels, index


class TimeSeriesDataset(Dataset):
    def __init__(self, args, data_x, pred_data_x, data_y, index):
        self.args = args
        self.data_x = data_x
        self.pred_data_x = pred_data_x
        self.data_y = data_y
        self.index = index

    def __getitem__(self, _index):
        return_x = self.data_x[_index]
        return_pred_x = self.pred_data_x[_index]

        return_y = -1
        if self.data_y is not None:
            return_y = self.data_y[_index]

        return_index = self.index[_index]

        return return_x, return_pred_x, return_y, return_index

    def __len__(self):
        return len(self.index)


def standardization(seqData, mean, std):
    return (seqData - mean) / std


def seed_worker(worker_id):
    worker_seed = torch.initial_seed() % 2**32
    np.random.seed(worker_seed)
    random.seed(worker_seed)


def fix_seed(random_seed):
    torch.manual_seed(random_seed)
    torch.cuda.manual_seed(random_seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(random_seed)
    random.seed(random_seed)


def main(args):
    fix_seed(args.random_seed)
    
    # load data
    train_X, _ = load_data(args.train_data_path)

    # split train data into train and validation set
    train_X, valid_X = train_test_split(train_X, test_size=0.3, shuffle=False)

    # data normalization
    train_mean = train_X.mean(dim=0)
    train_std = train_X.std(dim=0)

    train_X = standardization(train_X, train_mean, train_std)
    valid_X = standardization(valid_X, train_mean, train_std)

    trn_x, pred_trn_x, _, trn_ind = get_data_batch(train_X, data_y=None,
                                                   window_length=args.window_length,
                                                   sliding_step=args.sliding_step)
    val_x, pred_val_x, _, val_ind = get_data_batch(valid_X, data_y=None,
                                                   window_length=args.window_length,
                                                   sliding_step=args.sliding_step)

    # make Dataset
    trn_dataset = TimeSeriesDataset(args, trn_x, pred_trn_x, None, trn_ind)
    val_dataset = TimeSeriesDataset(args, val_x, pred_val_x, None, val_ind)

    # make DataLoader
    g = torch.Generator()
    g.manual_seed(args.random_seed)

    train_dataloader = DataLoader(dataset=trn_dataset, batch_size=args.batch_size, shuffle=True, drop_last=False, worker_init_fn=seed_worker, generator=g)
    valid_dataloader = DataLoader(dataset=val_dataset, batch_size=args.batch_size, shuffle=False, drop_last=False, worker_init_fn=seed_worker, generator=g)
    
    args.all_decode_masks = []
    for i in range(args.dec_nlayers):
        mask_i = np.random.randint(1, high=4, size=args.window_length, dtype='l')
        temp_mask = []
        for j in mask_i:
            if args.dec_use_skip:
                if j == 1:
                    temp_mask.append([0, 1])
                elif j == 2:
                    temp_mask.append([1, 0])
                else:
                    temp_mask.append([1, 1])
            else:
                temp_mask.append([1, 0])
        args.all_decode_masks.append(temp_mask)

    ratios = [1 / (args.tau ** (args.dec_nlayers - i - 1)) for i in range(args.dec_nlayers)]
    args.dec_Ls = np.random.randint(1, high=10 + 1, size=args.dec_nlayers, dtype='l')

    args.dec_lengths = []
    for i in range(args.dec_nlayers):
        args.dec_lengths.append(int(args.window_length * ratios[i]))

    model = RAE_MEPC(args)
    optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    # Train and validate model
    best_val_loss, best_mean, best_cov = None, None, None
    for epoch in range(1, args.num_epoch + 1):
        start_time = time.time()

        model.train()
        train_loss = 0
        for i, data in enumerate(train_dataloader):
            data_x, pred_data_x, _, _ = data
            data_x = data_x.to(device)
            pred_data_x = pred_data_x.to(device)

            loss, _, _, _ = model(data_x, pred_data_x)
            train_loss += loss.item()

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        train_loss /= (i + 1)

        model.eval()
        valid_loss = 0
        with torch.no_grad():
            for i, data in enumerate(valid_dataloader):
                data_x, pred_data_x, _, _ = data
                data_x = data_x.to(device)
                pred_data_x = pred_data_x.to(device)

                loss, _, _, _ = model(data_x, pred_data_x, mode='test')
                valid_loss += loss.item()

        valid_loss /= (i + 1)

        mean, cov = calculate_params(model, valid_dataloader, args.device)
        if best_val_loss is None or (best_val_loss - valid_loss) > 0:
            best_val_loss = valid_loss
            save_model_weights(model, args.model_save_path)
            best_mean = mean
            best_cov = cov

        del mean, cov

        if epoch == 1 or epoch % 100 == 0:
            print(
                '| end of epoch {:3d} | time: {:5.2f}s | train_loss {:5.4f} | best_valid_loss {:5.4f} |'.format(
                    epoch, (time.time() - start_time), train_loss, best_val_loss))

    save_mean_cov(best_mean, best_cov, args.mean_cov_save_path)



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--train-data-path', required=False, type=str)
    parser.add_argument('--save-root-path', type=str, default='./results')
    parser.add_argument('--lr', type=float, default=1e-3, help='the learning rate')
    parser.add_argument('--weight-decay', type=float, default=1e-6, help='weight decay')
    parser.add_argument('--num-epoch', type=int, default=500)
    parser.add_argument('--window-length', type=int, default=64)
    parser.add_argument('--sliding-step', type=int, default=64, help='stride to partition the sequence for training')
    parser.add_argument('--batch-size', type=int, default=128)
    parser.add_argument('--ninp', type=int, default=2, help='number of input variables')
    parser.add_argument('--hidden-size', type=int, default=64)
    parser.add_argument('--tau', type=int, default=4, help='tau')  # tau
    parser.add_argument('--nlayers', type=int, default=3)
    parser.add_argument('--enc-nlayers', type=int, default=3)
    parser.add_argument('--dec-nlayers', type=int, default=3)
    parser.add_argument('--dec-use-skip', action="store_true", default=False)
    parser.add_argument('--lambda-combine', type=float, default=0.1)  # beta
    parser.add_argument('--lambda-dtw', type=float, default=0.0001)  # lambda_shape
    parser.add_argument('--lambda-pred', type=float, default=1)  # lambda_pred
    parser.add_argument('--gamma', type=float, default=0.1)  # gamma in (10) from the paper
    parser.add_argument('--random-seed', type=int, default=42)

    args, _ = parser.parse_known_args()

    args.decay_ratio = 1 / args.tau
    args.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    args.model_save_path = os.path.join(args.save_root_path, 'checkpoint', 'model_{}_{}_{}_{}.pkl'.format(args.hidden_size,
                                                                                                          args.tau,
                                                                                                          args.lambda_combine,
                                                                                                          args.lambda_dtw))
    args.mean_cov_save_path = os.path.join(args.save_root_path, 'mean_cov', 'mean_cov_{}_{}_{}_{}.pkl'.format(args.hidden_size,
                                                                                                              args.tau,
                                                                                                              args.lambda_combine,
                                                                                                              args.lambda_dtw))

    os.makedirs(os.path.join(args.save_root_path, 'mean_cov'), exist_ok=True)
    os.makedirs(os.path.join(args.save_root_path, 'checkpoint'), exist_ok=True)

    main(args)
