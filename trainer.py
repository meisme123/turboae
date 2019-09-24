__author__ = 'yihanjiang'
import torch
import time
import torch.nn.functional as F

eps  = 1e-6

from utils import snr_sigma2db, snr_db2sigma, code_power, errors_ber_pos, errors_ber, errors_bler
from loss import customized_loss
from channels import generate_noise

import numpy as np
from knnie.knnie import kraskov_mi
######################################################################################
#
# Trainer, validation, and test for AE code design
#
######################################################################################

# trainer
def train(epoch, model, optimizer, args, use_cuda = False, verbose = True, mode = 'encoder'):

    device = torch.device("cuda" if use_cuda else "cpu")

    model.train()
    start_time = time.time()
    train_loss = 0.0

    for batch_idx in range(int(args.num_block/args.batch_size)):
        optimizer.zero_grad()
        X_train    = torch.randint(0, 2, (args.batch_size, args.block_len, args.code_rate_k), dtype=torch.float)

        # train encoder/decoder with different SNR... seems to be a good practice.
        if mode == 'encoder':
            fwd_noise  = generate_noise(X_train.shape, args, snr_low=args.train_channel_low, snr_high=args.train_channel_high, mode = 'encoder')
        else:
            fwd_noise  = generate_noise(X_train.shape, args, snr_low=args.train_dec_channel_low, snr_high=args.train_dec_channel_high, mode = 'decoder')

        X_train, fwd_noise = X_train.to(device), fwd_noise.to(device)

        output, code = model(X_train, fwd_noise)

        if mode == 'encoder':
            loss = customized_loss(output, X_train, args, noise=fwd_noise, code = code)
        else:
            output = torch.clamp(output, 0.0, 1.0)
            loss = F.binary_cross_entropy(output, X_train)

        loss.backward()
        train_loss += loss.item()

        optimizer.step()

    end_time = time.time()
    train_loss = train_loss /(args.num_block/args.batch_size)
    if verbose:
        print('====> Epoch: {} Average loss: {:.8f}'.format(epoch, train_loss), \
            ' running time', str(end_time - start_time))

    return train_loss

def validate(model, optimizer, args, use_cuda = False, verbose = True):

    device = torch.device("cuda" if use_cuda else "cpu")

    model.eval()
    test_bce_loss, test_custom_loss, test_ber= 0.0, 0.0, 0.0

    with torch.no_grad():
        num_test_batch = int(args.num_block/args.batch_size * args.test_ratio)
        for batch_idx in range(num_test_batch):
            X_test     = torch.randint(0, 2, (args.batch_size, args.block_len, args.code_rate_k), dtype=torch.float)
            fwd_noise  = generate_noise(X_test.shape, args, snr_low=args.train_channel_low, snr_high=args.train_channel_high)

            X_test, fwd_noise= X_test.to(device), fwd_noise.to(device)

            optimizer.zero_grad()
            output, codes = model(X_test, fwd_noise)

            output = torch.clamp(output, 0.0, 1.0)

            output = output.detach()
            X_test = X_test.detach()

            test_bce_loss += F.binary_cross_entropy(output, X_test)
            test_custom_loss += customized_loss(output, X_test, noise = fwd_noise, args = args, code = codes)
            test_ber  += errors_ber(output,X_test)


    test_bce_loss /= num_test_batch
    test_custom_loss /= num_test_batch
    test_ber  /= num_test_batch
    # test_MI  = compute_MI(codes.cpu().detach(), fwd_noise.cpu().detach())

    if verbose:
        print('====> Test set BCE loss', float(test_bce_loss),
              'Custom Loss',float(test_custom_loss),
              'with ber ', float(test_ber),
              #'with Mutual Information',float(test_MI)
        )

    report_loss = float(test_bce_loss)
    report_ber  = float(test_ber)
    #report_MI   = float(test_MI)


    return report_loss, report_ber, None


def test(model, args, use_cuda = False):

    device = torch.device("cuda" if use_cuda else "cpu")
    model.eval()

    ber_res, bler_res = [], []
    snr_interval = (args.snr_test_end - args.snr_test_start)* 1.0 /  (args.snr_points-1)
    snrs = [snr_interval* item + args.snr_test_start for item in range(args.snr_points)]
    print('SNRS', snrs)
    sigmas = snrs

    num_train_block =  args.num_block

    for sigma, this_snr in zip(sigmas, snrs):
        test_ber, test_bler = .0, .0
        with torch.no_grad():
            num_test_batch = int(num_train_block/(args.batch_size)* args.test_ratio)
            for batch_idx in range(num_test_batch):
                X_test     = torch.randint(0, 2, (args.batch_size, args.block_len, args.code_rate_k), dtype=torch.float)
                fwd_noise  = generate_noise(X_test.shape, args, test_sigma=sigma)

                X_test, fwd_noise= X_test.to(device), fwd_noise.to(device)

                X_hat_test, the_codes = model(X_test, fwd_noise)


                test_ber  += errors_ber(X_hat_test,X_test)
                test_bler += errors_bler(X_hat_test,X_test)

                if batch_idx == 0:
                    test_pos_ber = errors_ber_pos(X_hat_test,X_test)
                    codes_power  = code_power(the_codes)
                else:
                    test_pos_ber += errors_ber_pos(X_hat_test,X_test)
                    codes_power  += code_power(the_codes)

            if args.print_pos_power:
                print('code power', codes_power/num_test_batch)
            if args.print_pos_ber:
                print('positional ber', test_pos_ber/num_test_batch)

        test_ber  /= num_test_batch
        test_bler /= num_test_batch
        print('Test SNR',this_snr ,'with ber ', float(test_ber), 'with bler', float(test_bler))
        ber_res.append(float(test_ber))
        bler_res.append( float(test_bler))

    print('final results on SNRs ', snrs)
    print('BER', ber_res)
    print('BLER', bler_res)

    # compute adjusted SNR. (some quantization might make power!=1.0)
    enc_power = 0.0
    with torch.no_grad():
        for idx in range(num_test_batch):
            X_test     = torch.randint(0, 2, (args.batch_size, args.block_len, args.code_rate_k), dtype=torch.float)
            X_test     = X_test.to(device)
            X_code     = model.enc(X_test)
            enc_power +=  torch.std(X_code)
    enc_power /= float(num_test_batch)
    print('encoder power is',enc_power)
    adj_snrs = [snr_sigma2db(snr_db2sigma(item)/enc_power) for item in snrs]
    print('adjusted SNR should be',adj_snrs)



def compute_MI(codes, fwd_noise):
    codes = codes.numpy()
    fwd_noise = fwd_noise.numpy()

    recs = codes + fwd_noise
    Y = recs.reshape(recs.shape[0], recs.shape[1]*recs.shape[2],1 )
    X = codes.reshape(codes.shape[0], codes.shape[1]*codes.shape[2],1 )
    res = 0.0
    for idx in range(10):
        res += kraskov_mi(X[idx],Y[idx])/np.log(2.0)

    res /= 10

    return res














