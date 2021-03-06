# -*- coding: utf8 -*-
import os
import sys
import time
import json
import argparse
import random
# random.seed(49999)
import numpy
# numpy.random.seed(49999)
import tensorflow
# tensorflow.set_random_seed(49999)

from collections import OrderedDict

import keras
import keras.backend as K
from keras.models import Sequential, Model

from utils import *
import inputs
import metrics
from losses import *
import os.path
from tqdm import tqdm
import pickle
import pandas as pd
from scipy import stats
import time

def load_model(config):
    global_conf = config["global"]
    model_type = global_conf['model_type']
    if model_type == 'JSON':
        mo = Model.from_config(config['model'])
    elif model_type == 'PY':
        model_config = config['model']['setting']
        model_config.update(config['inputs']['share'])
        sys.path.insert(0, config['model']['model_path'])

        model = import_object(config['model']['model_py'], model_config)
        mo = model.build()
    return mo


def train(config):

    print(json.dumps(config, indent=2))
    # read basic config
    global_conf = config["global"]
    optimizer = global_conf['optimizer']
    weights_file = str(global_conf['weights_file']) + '.%d'
    display_interval = int(global_conf['display_interval'])
    num_iters = int(global_conf['num_iters'])
    save_weights_iters = int(global_conf['save_weights_iters'])

    # read input config
    input_conf = config['inputs']
    share_input_conf = input_conf['share']

    if 'keras_random_seed' in share_input_conf:
        tensorflow.set_random_seed(share_input_conf['keras_random_seed'])
        random.seed(share_input_conf['keras_random_seed'])
        numpy.random.seed(share_input_conf['keras_random_seed'])
        print("Using random seed: " + str(share_input_conf['keras_random_seed']))

    # collect embedding
    if 'embed_path' in share_input_conf:
        embed_dict = read_embedding(filename=share_input_conf['embed_path'])
        _PAD_ = share_input_conf['vocab_size'] - 1
        embed_dict[_PAD_] = np.zeros((share_input_conf['embed_size'], ), dtype=np.float32)
        embed = np.float32(np.random.uniform(-0.2, 0.2, [share_input_conf['vocab_size'], share_input_conf['embed_size']]))
        share_input_conf['embed'] = convert_embed_2_numpy(embed_dict, embed = embed)
    else:
        embed = np.float32(np.random.uniform(-0.2, 0.2, [share_input_conf['vocab_size'], share_input_conf['embed_size']]))
        share_input_conf['embed'] = embed
    print '[Embedding] Embedding Load Done.'

    # list all input tags and construct tags config
    input_train_conf = OrderedDict()
    input_eval_conf = OrderedDict()
    for tag in input_conf.keys():
        if 'phase' not in input_conf[tag]:
            continue
        if input_conf[tag]['phase'] == 'TRAIN':
            input_train_conf[tag] = {}
            input_train_conf[tag].update(share_input_conf)
            input_train_conf[tag].update(input_conf[tag])
        elif input_conf[tag]['phase'] == 'EVAL':
            input_eval_conf[tag] = {}
            input_eval_conf[tag].update(share_input_conf)
            input_eval_conf[tag].update(input_conf[tag])
    # print '[Input] Process Input Tags. %s in TRAIN, %s in EVAL.' % (input_train_conf.keys(), input_eval_conf.keys())

    # collect dataset identification
    dataset = {}
    for tag in input_conf:
        if tag != 'share' and input_conf[tag]['phase'] == 'PREDICT':
            continue
        if 'text1_corpus' in input_conf[tag]:
            datapath = input_conf[tag]['text1_corpus']
            if datapath not in dataset:
                dataset[datapath] = read_data_2d(datapath)
        if 'text2_corpus' in input_conf[tag]:
            datapath = input_conf[tag]['text2_corpus']
            if datapath not in dataset:
                dataset[datapath] = read_data_2d(datapath)
        if 'qa_comat_file' in input_conf[tag]: # qa_comat_file for qa_cooccur_matrix in DMN_KD
            datapath = input_conf[tag]['qa_comat_file']
            if datapath not in dataset:
                dataset[datapath] = read_qa_comat(datapath)
        if (share_input_conf["predict_ood"] or ('train_clf_with_ood' in share_input_conf and share_input_conf['train_clf_with_ood'])):
            if 'text1_corpus_ood' in input_conf[tag]:
                datapath = input_conf[tag]['text1_corpus_ood']
                if datapath not in dataset:
                    dataset[datapath] = read_data_2d(datapath)
            if 'text2_corpus_ood' in input_conf[tag]:
                datapath = input_conf[tag]['text2_corpus_ood']
                if datapath not in dataset:
                    dataset[datapath] = read_data_2d(datapath)  
    print '[Dataset] %s Dataset Load Done.' % len(dataset)

    # initial data generator
    train_gen = OrderedDict()
    eval_gen = OrderedDict()

    for tag, conf in input_train_conf.items():
        # print conf
        conf['data1'] = dataset[conf['text1_corpus']]
        conf['data2'] = dataset[conf['text2_corpus']]
        if 'qa_comat_file' in share_input_conf:
            conf['qa_comat'] = dataset[conf['qa_comat_file']]
        generator = inputs.get(conf['input_type'])

        if(tag == 'train_clf' and 'train_clf_with_ood' in share_input_conf and share_input_conf['train_clf_with_ood']):
            conf['data1_ood'] = dataset[input_eval_conf['eval_predict_in']['text1_corpus_ood']]
            conf['data2_ood'] = dataset[input_eval_conf['eval_predict_in']['text1_corpus_ood']]
        train_gen[tag] = generator( config = conf )

    for tag, conf in input_eval_conf.items():
        # print conf
        conf['data1'] = dataset[conf['text1_corpus']]
        conf['data2'] = dataset[conf['text2_corpus']]
        if (share_input_conf["predict_ood"]):
            conf['data1_ood'] = dataset[conf['text1_corpus_ood']]
            conf['data2_ood'] = dataset[conf['text2_corpus_ood']]
        if 'qa_comat_file' in share_input_conf:
            conf['qa_comat'] = dataset[conf['qa_comat_file']]
        generator = inputs.get(conf['input_type'])
        eval_gen[tag] = generator( config = conf )

    ######### Load Model #########
    model, model_clf, lambda_var = load_model(config)
    to_load_weights_file_ = str(global_conf['weights_file']) + '.' + str(global_conf['test_weights_iters'])
    offset = 0
    # if(os.path.isfile(to_load_weights_file_)):
    #     print "loading weights from file "+to_load_weights_file_
    #     model.load_weights(to_load_weights_file_)
    #     offset=global_conf['test_weights_iters']

    loss = []
    for lobj in config['losses']:
        if lobj['object_name'] in mz_specialized_losses:
            loss.append(rank_losses.get(lobj['object_name'])(lobj['object_params']))
        else:
            loss.append(rank_losses.get(lobj['object_name']))
    eval_metrics = OrderedDict()
    for mobj in config['metrics']:
        mobj = mobj.lower()
        if '@' in mobj:
            mt_key, mt_val = mobj.split('@', 1)
            eval_metrics[mobj] = metrics.get(mt_key)(int(mt_val))
        else:
            eval_metrics[mobj] = metrics.get(mobj)
    model.compile(optimizer=optimizer, loss=loss)
    print '[Model] Model Compile Done.'

    def custom_loss(y_true, y_pred):
        cce = categorical_crossentropy(y_true, y_pred)
        lambda_domain_loss = 1.0
        return cce * lambda_domain_loss

    model_clf.compile(optimizer=optimizer, loss=custom_loss)
    print '[Model] Domain classifier model Compile Done.'
    # print(model_clf.summary())

    if(share_input_conf['predict'] == 'False'):
        if('test' in eval_gen):
            del eval_gen['test']
        if('valid' in eval_gen):
            del eval_gen['valid']
        if('eval_predict_in' in eval_gen):
            del eval_gen['eval_predict_in']

    if(share_input_conf["domain_training_type"] != "DMN-ADL" and \
        share_input_conf["domain_training_type"] != "DMN-MTL" and 'train_clf' in train_gen):
        del train_gen['train_clf']

    if('l' in share_input_conf):
        print("Using lambda : ", share_input_conf['l'])
    alternate_per_batch = False
    if(alternate_per_batch):
        print("training alternated batches.")
    initial_clf_weights = model_clf.layers[-1].get_weights()
    for i_e in range(num_iters):
        if('reset_clf_weights_iters' in share_input_conf):
            if(i_e+1) % share_input_conf['reset_clf_weights_iters'] == 0:
                print("Resetting clf dense layer weights.")
                model_clf.layers[-1].set_weights(initial_clf_weights)

        if(alternate_per_batch and (share_input_conf["domain_training_type"] == "DMN-ADL" \
            or share_input_conf["domain_training_type"] == "DMN-MTL")):
            for i in range(display_interval):
                for tag, generator in train_gen.items():
                    genfun = generator.get_batch_generator()
                    # print '[%s]\t[Train:%s]' % (time.strftime('%m-%d-%Y %H:%M:%S', time.localtime(time.time())), tag),
                    print('Train '+tag)
                    if(tag == "train_clf"):
                        correct_model = model_clf
                        p = float(i_e) / num_iters
                        if('l' in share_input_conf):
                            l = share_input_conf['l']
                        else:
                            l = 2. / (1. + np.exp(-10. * p)) - 1
                        K.set_value(lambda_var, K.cast_to_floatx(l))
                    elif(tag == "train"):
                        correct_model = model
                    history = correct_model.fit_generator(
                            genfun,
                            steps_per_epoch = 1,
                            epochs = 1,
                            shuffle=False,
                            verbose = 0
                        ) #callbacks=[eval_map])
                    if(i==(display_interval-1)):
                        print ("Iter : "+ str(i_e) + " loss="+str(history.history['loss'][0]))
        else:
            for tag, generator in train_gen.items():
                genfun = generator.get_batch_generator()
                print '[%s]\t[Train:%s]' % (time.strftime('%m-%d-%Y %H:%M:%S', time.localtime(time.time())), tag),

                if(tag == "train_clf"):
                    correct_model = model_clf 
                    p = float(i_e) / num_iters
                    if('l' in share_input_conf):
                        l = share_input_conf['l']
                    else:
                        l = 2. / (1. + np.exp(-10. * p)) - 1
                    K.set_value(lambda_var, K.cast_to_floatx(l))
                elif(tag == "train"):
                    correct_model = model
                history = correct_model.fit_generator(
                        genfun,
                        steps_per_epoch = display_interval, # if display_interval = 10, then there are 10 batches in 1 epoch
                        epochs = 1,
                        shuffle=False,
                        verbose = 0)

                # if(tag == "train_clf"):
                #     from IPython import embed
                #     embed()
                #     weights = model_clf.trainable_weights
                #     gradients = model_clf.optimizer.get_gradients(model_clf.total_loss, weights) # gradient tensors

                #     input_tensors = [model_clf.input[0], # input data
                #      model_clf.input[1], # input data
                #      model_clf.sample_weights[0], # how much to weight each sample by
                #      model_clf.targets[0], # labels
                #      K.learning_phase(), # train or test mode
                #     ]
                #     from keras.utils.np_utils import to_categorical
                #     for input_data, y_true in genfun:
                #         get_gradients = K.function(inputs=input_tensors, outputs=gradients[-8:])
                #         input_v = [input_data['query'], # X
                #                   input_data['doc'],
                #                   np.array([1] * len(input_data['query'])), # sample weights
                #                   y_true, # y
                #                   0 # learning phase in TEST mode
                #         ]
                #         break
                #     results = zip(weights, get_gradients(input_v))
                print 'Iter:%d\tloss=%.6f' % (i_e, history.history['loss'][0])

        for tag, generator in eval_gen.items():
            genfun = generator.get_batch_generator()
            print '[%s]\t[Eval:%s]' % (time.strftime('%m-%d-%Y %H:%M:%S', time.localtime(time.time())), tag),
            res = dict([[k,0.] for k in eval_metrics.keys()])
            num_valid = 0
            for input_data, y_true in genfun:
                y_pred = model.predict(input_data, batch_size=len(y_true))
                if issubclass(type(generator), inputs.list_generator.ListBasicGenerator) or \
                    issubclass(type(generator), inputs.list_generator.ListOODGenerator) or \
                    issubclass(type(generator), inputs.list_generator.ListTopicsGenerator):
                    list_counts = input_data['list_counts']
                    for k, eval_func in eval_metrics.items():
                        for lc_idx in range(len(list_counts)-1):
                            pre = list_counts[lc_idx]
                            suf = list_counts[lc_idx+1]
                            res[k] += eval_func(y_true = y_true[pre:suf], y_pred = y_pred[pre:suf])
                    num_valid += len(list_counts) - 1
                else:
                    for k, eval_func in eval_metrics.items():
                        res[k] += eval_func(y_true = y_true, y_pred = y_pred)
                    num_valid += 1
            generator.reset()
            print 'Iter:%d\t%s' % (i_e, '\t'.join(['%s=%f'%(k,v/num_valid) for k, v in res.items()]))
            sys.stdout.flush()

        if (i_e+1) % save_weights_iters == 0:
            path_to_save = weights_file
            # if('domain_to_train' in input_conf['train'] and input_conf['train']['domain_to_train'] != -1):

            #training on multiple domain dataset
            if('train' in input_conf and 'domain_to_train' in input_conf['train']):
                path_to_save = weights_file+str(input_conf['train']['domain_to_train']+1)*5
                if(share_input_conf["domain_training_type"] == "DMN-ADL"):
                    model.save_weights(path_to_save % (i_e+offset+1+1000))
                    if('input_to_domain_clf' in share_input_conf):
                        path_to_save = path_to_save+'_'+share_input_conf['input_to_domain_clf']
                        model.save_weights(path_to_save % (i_e+offset+1+1000))
                elif(share_input_conf["domain_training_type"] == "DMN-MTL"):
                    model.save_weights(path_to_save % (i_e+offset+1+2000))
                    if('input_to_domain_clf' in share_input_conf):
                        path_to_save = path_to_save+'_'+share_input_conf['input_to_domain_clf']
                        model.save_weights(path_to_save % (i_e+offset+1+1000))
                else:
                    model.save_weights(path_to_save % (i_e+offset+1))

            #training only on one domain datasets:
            if(share_input_conf["domain_training_type"] == "DMN-ADL"):
                model.save_weights(weights_file % (i_e+offset+1+1000))
            elif(share_input_conf["domain_training_type"] == "DMN-MTL"):
                model.save_weights(weights_file % (i_e+offset+1+2000))
            else:
                model.save_weights(weights_file % (i_e+offset+1))

def predict(config):
    ######## Read input config ########

    print(json.dumps(config, indent=2))
    input_conf = config['inputs']
    share_input_conf = input_conf['share']

    # collect embedding
    if 'embed_path' in share_input_conf:
        embed_dict = read_embedding(filename=share_input_conf['embed_path'])
        _PAD_ = share_input_conf['vocab_size'] - 1
        embed_dict[_PAD_] = np.zeros((share_input_conf['embed_size'], ), dtype=np.float32)
        embed = np.float32(np.random.uniform(-0.02, 0.02, [share_input_conf['vocab_size'], share_input_conf['embed_size']]))
        share_input_conf['embed'] = convert_embed_2_numpy(embed_dict, embed = embed)
    else:
        embed = np.float32(np.random.uniform(-0.2, 0.2, [share_input_conf['vocab_size'], share_input_conf['embed_size']]))
        share_input_conf['embed'] = embed
    print '[Embedding] Embedding Load Done.'

    # list all input tags and construct tags config
    input_predict_conf = OrderedDict()
    for tag in input_conf.keys():
        if 'phase' not in input_conf[tag]:
            continue
        if input_conf[tag]['phase'] == 'PREDICT':
            input_predict_conf[tag] = {}
            input_predict_conf[tag].update(share_input_conf)
            input_predict_conf[tag].update(input_conf[tag])
    print '[Input] Process Input Tags. %s in PREDICT.' % (input_predict_conf.keys())

    # collect dataset identification
    dataset = {}
    for tag in input_conf:
        if tag == 'share' or input_conf[tag]['phase'] == 'PREDICT':
            if 'text1_corpus' in input_conf[tag]:
                datapath = input_conf[tag]['text1_corpus']
                if datapath not in dataset:
                    dataset[datapath] = read_data_2d(datapath)
            if 'text2_corpus' in input_conf[tag]:
                datapath = input_conf[tag]['text2_corpus']
                if datapath not in dataset:
                    dataset[datapath] = read_data_2d(datapath)
            if 'qa_comat_file' in input_conf[tag]:  # qa_comat_file for qa_cooccur_matrix in DMN_KD_CQA and DMN_KD_Web
                datapath = input_conf[tag]['qa_comat_file']
                if datapath not in dataset:
                    dataset[datapath] = read_qa_comat(datapath)
            if (share_input_conf["predict_ood"]):
                if 'text1_corpus_ood' in input_conf[tag]:
                    datapath = input_conf[tag]['text1_corpus_ood']
                    if datapath not in dataset:
                        dataset[datapath] = read_data_2d(datapath)
                if 'text2_corpus_ood' in input_conf[tag]:
                    datapath = input_conf[tag]['text2_corpus_ood']
                    if datapath not in dataset:
                        dataset[datapath] = read_data_2d(datapath)            
    print '[Dataset] %s Dataset Load Done.' % len(dataset)

    # initial data generator
    predict_gen = OrderedDict()

    for tag, conf in input_predict_conf.items():
        if(tag == "predict_ood" and not share_input_conf["predict_ood"]):
            continue
        conf['data1'] = dataset[conf['text1_corpus']]
        conf['data2'] = dataset[conf['text2_corpus']]
        if (share_input_conf["predict_ood"]):
            conf['data1_ood'] = dataset[conf['text1_corpus_ood']]
            conf['data2_ood'] = dataset[conf['text2_corpus_ood']]
        if 'qa_comat_file' in share_input_conf:
            conf['qa_comat'] = dataset[conf['qa_comat_file']]
        generator = inputs.get(conf['input_type'])
        predict_gen[tag] = generator(
                                    #data1 = dataset[conf['text1_corpus']],
                                    #data2 = dataset[conf['text2_corpus']],
                                     config = conf )

    ######## Read output config ########
    output_conf = config['outputs']

    ######## Load Model ########
    global_conf = config["global"]

    if('random_weights_predict' in share_input_conf and share_input_conf['random_weights_predict']):
        tensorflow.set_random_seed(int(time.time()))
        model, model_clf, _ = load_model(config)
        print("Using random weights")
    else:        
        model, model_clf, _ = load_model(config)
        weights_file = str(global_conf['weights_file']) + '.' + str(global_conf['test_weights_iters'])
        model.load_weights(weights_file)
        print ('Model loaded')
    # print(model.summary())

    eval_metrics = OrderedDict()
    for mobj in config['metrics']:
        mobj = mobj.lower()
        if '@' in mobj:
            mt_key, mt_val = mobj.split('@', 1)
            eval_metrics[mobj] = metrics.get(mt_key)(int(mt_val))
        else:
            eval_metrics[mobj] = metrics.get(mobj)

    save_query_representation=False
    if 'save_query_representation' in share_input_conf:
        save_query_representation = True
    if(save_query_representation):
        utterances_w_emb = {}
    print(predict_gen)

    for tag, generator in predict_gen.items():
        res = dict([[k,0.] for k in eval_metrics.keys()])
        genfun = generator.get_batch_generator()
        print '[%s]\t[Predict] @ %s ' % (time.strftime('%m-%d-%Y %H:%M:%S', time.localtime(time.time())), tag),
        num_valid = 0
        res_scores = {}
        # pbar = tqdm(total=generator.num_list)
        for input_data, y_true in genfun:
            y_pred = model.predict(input_data, batch_size=len(y_true))

            if(save_query_representation):
                if(share_input_conf['save_query_representation'] == 'match'):
                    # match representations
                    match_representation_layer_model = Model(inputs=model.input,
                                                             outputs=model.get_layer('reshape_'+str(config['inputs']['share']['text1_max_utt_num']+1)).output)
                    batch_match_embedding = match_representation_layer_model.predict(input_data, batch_size=len(y_true))
                    list_counts = input_data['list_counts']
                    for lc_idx in range(len(list_counts)-1):
                        pre = list_counts[lc_idx]
                        suf = list_counts[lc_idx+1]
                        q = input_data['ID'][pre:pre+1][0][0]
                        if(tag == 'predict_ood'):
                            q = ('Q'+str(9900000+ int(q.split('Q')[1])))
                        if(q not in utterances_w_emb):
                            utterances_w_emb[q] = {}
                        utterances_w_emb[q]['match_rep'] = batch_match_embedding[pre:pre+1]
                elif(share_input_conf['save_query_representation'] == 'sentence'):
                    # GRU sentence representations
                    utterances_bigru = []
                    for i in range(config['inputs']['share']['text1_max_utt_num'] * 2):
                        if((i+1)%2!=0):
                            # print(i+1)
                            intermediate_layer_model = Model(inputs=model.input,
                                                             outputs=model.get_layer('bidirectional_'+str(i+1)).output)
                            utterances_bigru.append(intermediate_layer_model.predict(input_data, batch_size=len(y_true)))

                    list_counts = input_data['list_counts']
                    for lc_idx in range(len(list_counts)-1):
                        pre = list_counts[lc_idx]
                        suf = list_counts[lc_idx+1]
                        q = input_data['ID'][pre:pre+1][0][0]
                        if(tag == 'predict_ood'):
                            q = ('Q'+str(9900000+ int(q.split('Q')[1])))
                        if(q not in utterances_w_emb):
                            utterances_w_emb[q] = {}
                        for i in range(len(utterances_bigru)):
                            turn_bigru = utterances_bigru[i]
                            turn_bigru = turn_bigru.reshape(turn_bigru.shape[0],-1)
                            utterances_w_emb[q]['turn_'+str(i+1)+'_bigru'] = turn_bigru[pre:pre+1]
                elif(share_input_conf['save_query_representation'] == 'text'):
                    #Word embedding sentence representations
                    # for i in [0]: #range(config['inputs']['share']['text1_max_utt_num']):
                    for i in range(config['inputs']['share']['text1_max_utt_num']):
                        intermediate_layer_model = Model(inputs=model.input,
                                                         outputs=model.get_layer('embedding_1').get_output_at(i+1))
                        batch_embeddings = intermediate_layer_model.predict(input_data, batch_size=len(y_true))
                        batch_embeddings = batch_embeddings.reshape(batch_embeddings.shape[0],-1)
                        list_counts = input_data['list_counts']
                        for lc_idx in range(len(list_counts)-1):
                            pre = list_counts[lc_idx]
                            suf = list_counts[lc_idx+1]
                            q = input_data['ID'][pre:pre+1][0][0]
                            if(tag == 'predict_ood'):
                                q = ('Q'+str(9900000+ int(q.split('Q')[1])))
                            if(q not in utterances_w_emb):
                                utterances_w_emb[q] = {}
                            utterances_w_emb[q]['turn_'+str(i+1)] = batch_embeddings[pre:pre+1]

            if issubclass(type(generator), inputs.list_generator.ListBasicGenerator) or  \
                issubclass(type(generator), inputs.list_generator.ListOODGenerator) or \
                issubclass(type(generator), inputs.list_generator.ListTopicsGenerator):
                list_counts = input_data['list_counts']
                for k, eval_func in eval_metrics.items():
                    for lc_idx in range(len(list_counts)-1):
                        pre = list_counts[lc_idx]
                        suf = list_counts[lc_idx+1]
                        res[k] += eval_func(y_true = y_true[pre:suf], y_pred = y_pred[pre:suf])

                y_pred = np.squeeze(y_pred)
                for lc_idx in range(len(list_counts)-1):
                    pre = list_counts[lc_idx]
                    suf = list_counts[lc_idx+1]
                    for p, y, t in zip(input_data['ID'][pre:suf], y_pred[pre:suf], y_true[pre:suf]):
                        q = p[0]
                        if(tag == 'predict_ood'):
                            q = ('Q'+str(9900000+ int(q.split('Q')[1])))
                        if q not in res_scores:
                            res_scores[q] = {}
                        res_scores[q][p[1]] = (y, t)

                num_valid += len(list_counts) - 1
                if(save_query_representation and num_valid > 3479):
                    break
            else:
                for k, eval_func in eval_metrics.items():
                    res[k] += eval_func(y_true = y_true, y_pred = y_pred)
                for p, y, t in zip(input_data['ID'], y_pred, y_true):
                    if p[0] not in res_scores:
                        res_scores[p[0]] = {}
                    res_scores[p[0]][p[1]] = (y[1], t[1])
                num_valid += 1
            # if('predict' in config['inputs']):
            #     pbar.update(config['inputs']['predict']['batch_list'])
            # elif('predict_in'in config['inputs']):
            #     pbar.update(config['inputs']['predict_in']['batch_list'])
            # elif('predict_out'in config['inputs']):
            #     pbar.update(config['inputs']['predict_out']['batch_list'])
            # elif('predict_ood'in config['inputs']):
            #     pbar.update(config['inputs']['predict_ood']['batch_list'])

        generator.reset()

        pvalue_sufix=""
        if(save_query_representation):
            with open(config['global']['representations_save_path']+'q_rep.pickle', 'wb') as handle:
                pickle.dump(utterances_w_emb, handle, protocol=pickle.HIGHEST_PROTOCOL)
        if tag in output_conf:
            if(tag == "predict_ood" and not share_input_conf["predict_ood"]):
                continue
            if output_conf[tag]['save_format'] == 'TREC':
                suffix = ""                
                if(len(str(global_conf['test_weights_iters']))==9):
                    if(str(global_conf['test_weights_iters'])[0]=='1'):
                        suffix="_ADL"
                    else:
                        suffix="_MTL"
                    if(str(global_conf['test_weights_iters'])[-5:] == '11111'):
                        suffix+="_trained_on_domain_1"
                    elif(str(global_conf['test_weights_iters'])[-5:] == '22222'):
                        suffix+="_trained_on_domain_2"
                    else:
                        suffix+="_trained_on_both"

                with open(output_conf[tag]['save_path']+suffix, 'w') as f:
                    for qid, dinfo in res_scores.items():
                        dinfo = sorted(dinfo.items(), key=lambda d:d[1][0], reverse=True)
                        for inum,(did, (score, gt)) in enumerate(dinfo):
                            print >> f, '%s\tQ0\t%s\t%d\t%f\t%s\t%s'%(qid, did, inum, score, config['net_name'], gt)
            elif output_conf[tag]['save_format'] == 'TEXTNET':
                with open(output_conf[tag]['save_path'], 'w') as f:
                    for qid, dinfo in res_scores.items():
                        dinfo = sorted(dinfo.items(), key=lambda d:d[1][0], reverse=True)
                        for inum,(did, (score, gt)) in enumerate(dinfo):
                            print >> f, '%s %s %s %s'%(gt, qid, did, score)

            pvalue_sufix=""
            if('statistical_test' in share_input_conf and share_input_conf['statistical_test'] == 't-test'\
                and tag in ['predict_in', 'predict_ood', 'predict_out']):

                file_baseline = output_conf[tag]['save_path']
                file_current_model = output_conf[tag]['save_path']+suffix
                print('baseline file: ' + file_baseline)
                print('file current model:' + file_current_model)
                res_baseline = pd.read_csv(file_baseline, \
                    sep="\t", names=["Q","_", "D", "rank", "score", "model", "label"])
                res_current_model = pd.read_csv(file_current_model, \
                    sep="\t", names=["Q","_", "D", "rank", "score", "model", "label"])

                calc_metric = metrics.get('calculate_map')

                df_ap_baseline = res_baseline.groupby(["Q"])['label','score']\
                    .apply(lambda r,f = calc_metric: f(r)).reset_index()
                df_ap_baseline.columns = ["Q", "ap_baseline"]

                df_ap_current_model = res_current_model.groupby(["Q"])['label','score']\
                    .apply(lambda r,f = calc_metric: f(r)).reset_index()
                df_ap_current_model.columns = ["Q", "ap_current_model"]

                df_ap_both = df_ap_baseline.merge(df_ap_current_model, on='Q')

                statistic, pvalue = stats.ttest_rel(df_ap_both['ap_baseline'], df_ap_both['ap_current_model'])
                print('map pvalue '+str(pvalue))
                print('map statistic '+str(statistic))

                calc_metric = metrics.get('calculate_ap_1')

                df_p_baseline = res_baseline.groupby(["Q"])['label','score']\
                    .apply(lambda r,f = calc_metric: f(r)).reset_index()
                df_p_baseline.columns = ["Q", "p1_baseline"]

                df_p_current_model = res_current_model.groupby(["Q"])['label','score']\
                    .apply(lambda r,f = calc_metric: f(r)).reset_index()
                df_p_current_model.columns = ["Q", "p1_current_model"]

                df_p_both = df_p_baseline.merge(df_p_current_model, on='Q')

                statistic, pvalue = stats.ttest_rel(df_p_both['p1_baseline'], df_p_both['p1_current_model'])
                print('p@1 pvalue '+str(pvalue))
                print('p@1 statistic '+str(statistic))
        print("valids:", num_valid)
        print '[Predict] results: ', '\t'.join(['%s=%f'%(k,v/num_valid) for k, v in res.items()]), pvalue_sufix
        sys.stdout.flush()


def main(argv):
    parser = argparse.ArgumentParser()
    # python main_conversation_qa.py --help to print the help messages
    # sys.argv includes a list of elements starting with the program
    # required parameters
    parser.add_argument('--phase', default='train', help='Phase: Can be train or predict, the default value is train.', required=True)
    parser.add_argument('--model_file', default='./models/arci.config', help='Model_file: MatchZoo model file for the chosen model.', required=True)
    parser.add_argument('--or_cmd', default=False,
                        help='or_cmd: whether want to override config parameters by command line parameters', required=True)

    # optional parameters
    parser.add_argument('--embed_size', help='Embed_size: number of dimensions in word embeddings.')
    parser.add_argument('--embed_path', help='Embed_path: path of embedding file.')
    parser.add_argument('--test_relation_file', help='test_relation_file: path of test relation file.')
    parser.add_argument('--predict_relation_file', help='predict_relation_file: path of predict relation file.')
    parser.add_argument('--train_relation_file', help='train_relation_file: path of train relation file.')
    parser.add_argument('--valid_relation_file', help='valid_relation_file: path of valid relation file.')
    parser.add_argument('--vocab_size', help='vocab_size: vocab size')
    parser.add_argument('--text1_corpus', help='text1_corpus: path of text1 corpus')
    parser.add_argument('--text2_corpus', help='text2_corpus: path of text2 corpus')
    parser.add_argument('--weights_file', help='weights_file: path of weights file')
    parser.add_argument('--save_path', help='save_path: path of predicted score file')
    parser.add_argument('--valid_batch_list', help='valid_batch_list: batch size in valid data')
    parser.add_argument('--test_batch_list', help='test_batch_list: batch size in test data')
    parser.add_argument('--predict_batch_list', help='predict_batch_list: batch size in test data')
    parser.add_argument('--train_batch_size', help='train_batch_size: batch size in train data')
    parser.add_argument('--text1_max_utt_num', help='text1_max_utt_num: max number of utterances in dialog context')
    parser.add_argument('--cross_matrix', help='cross_matrix: parameters for model abalation')
    parser.add_argument('--inter_type', help='inter_type: parameters for model abalation')
    parser.add_argument('--test_weights_iters', help='test_weights_iters: the iteration of test weights file used')
    parser.add_argument('--predict_ood', help='whether to predict on out-of-domain or not')
    parser.add_argument('--predict', help='whether to predict (EVAL) on while training or not')
    parser.add_argument('--domain_training_type', help='wheter to use DMN-ADL, DMN-MTL or none')
    parser.add_argument('--domain_to_train', help='train in only one source domain or all (-1)')
    parser.add_argument('--num_iters', help='number of iters')
    parser.add_argument('--test_category', help='used for setting the out of domain topic for MSDialog topic as domain experiments')
    parser.add_argument('--input_to_domain_clf', help='whether to use <query_doc> representations or <match> representations')
    parser.add_argument('--statistical_test', help='test against baseline or not')
    parser.add_argument('--reset_clf_weights_iters', help='if set to a value the domain clf weights will reset every <reset_clf_weights_iters> iterations')
    parser.add_argument('--train_clf_with_ood', help='use ood instances for training clf (to be used with training on both source domains)')
    parser.add_argument('--save_query_representation', help='used in predict to save the query representations either <text> or <match>')
    parser.add_argument('--random_weights_predict', help='checked only on phase predict, wheter to use random weights or not')
    parser.add_argument('--keras_random_seed', help='the random seed to use in keras')
    parser.add_argument('--l', help='parameter between [0,1] that controls how much to regularize DMN with MTL/ADL')
    parser.add_argument('--test_categories', help='categories to filter for target domain, separated by ,')


    args = parser.parse_args()
    # parse the hyper-parameters from the command lines
    phase = args.phase
    model_file =  args.model_file
    or_cmd = bool(args.or_cmd)

    # load settings from the config file
    # then update the hyper-parameters in the config files with the settings passed from command lines
    with open(model_file, 'r') as f:
        config = json.load(f)
    if or_cmd:
        embed_size = args.embed_size
        embed_path = args.embed_path
        test_relation_file = args.test_relation_file
        predict_relation_file = args.predict_relation_file
        train_relation_file = args.train_relation_file
        valid_relation_file = args.valid_relation_file
        vocab_size = args.vocab_size
        text1_corpus = args.text1_corpus
        text2_corpus = args.text2_corpus
        weights_file = args.weights_file
        save_path = args.save_path
        text1_max_utt_num = args.text1_max_utt_num
        valid_batch_list = args.valid_batch_list
        predict_batch_list = args.predict_batch_list
        test_batch_list = args.test_batch_list
        train_batch_size = args.train_batch_size
        cross_matrix = args.cross_matrix
        inter_type = args.inter_type
        test_weights_iters = args.test_weights_iters
        predict_ood = args.predict_ood
        predict_eval = args.predict
        domain_training_type = args.domain_training_type
        domain_to_train = args.domain_to_train
        num_iters = args.num_iters
        test_category = args.test_category
        input_to_domain_clf = args.input_to_domain_clf
        statistical_test = args.statistical_test
        reset_clf_weights_iters = args.reset_clf_weights_iters
        train_clf_with_ood = args.train_clf_with_ood
        save_query_representation = args.save_query_representation
        random_weights_predict = args.random_weights_predict
        keras_random_seed = args.keras_random_seed
        l = args.l
        test_categories = args.test_categories

        if test_categories != None:
            config['inputs']['share']['test_categories'] = test_categories
        if l != None:
            config['inputs']['share']['l'] = float(l)
        if keras_random_seed != None:
            config['inputs']['share']['keras_random_seed'] = int(keras_random_seed)
        if random_weights_predict != None:
            config['inputs']['share']['random_weights_predict'] = random_weights_predict == 'True'
        if save_query_representation != None:
            config['inputs']['share']['save_query_representation'] = save_query_representation
        if train_clf_with_ood != None:
            config['inputs']['share']['train_clf_with_ood'] = train_clf_with_ood == 'True'
            if config['inputs']['share']['train_clf_with_ood']:
                config['inputs']['share']['relation_file_ood'] = config['inputs']['predict_ood']['relation_file_ood']
        if reset_clf_weights_iters != None:
            config['inputs']['share']['reset_clf_weights_iters'] = int(reset_clf_weights_iters)
        if statistical_test != None:
            config['inputs']['share']['statistical_test'] = statistical_test
        if input_to_domain_clf != None:
            config['inputs']['share']['input_to_domain_clf'] = input_to_domain_clf
        if test_category != None:
            config['inputs']['share']['test_category'] = test_category
        if num_iters != None:
            config['global']['num_iters'] = int(num_iters)
        if domain_to_train != None:
            config['inputs']['train']['domain_to_train'] = int(domain_to_train)
        if domain_training_type != None:
            config['inputs']['share']['domain_training_type'] = domain_training_type
        if predict_eval != None:
            config['inputs']['share']['predict'] = predict_eval
        if predict_ood != None:
            config['inputs']['share']['predict_ood'] = predict_ood == 'True'
        if embed_size != None:
            config['inputs']['share']['embed_size'] = int(embed_size)
        if embed_path != None:
            config['inputs']['share']['embed_path'] = embed_path
        if cross_matrix != None:
            config['inputs']['share']['cross_matrix'] = cross_matrix
        if inter_type != None:
            config['inputs']['share']['inter_type'] = inter_type
        if test_relation_file != None:
            config['inputs']['test']['relation_file'] = test_relation_file
        if predict_relation_file != None:
            config['inputs']['predict']['relation_file'] = predict_relation_file
        if train_relation_file != None:
            config['inputs']['train']['relation_file'] = train_relation_file
        if valid_relation_file != None:
            config['inputs']['valid']['relation_file'] = valid_relation_file
        if vocab_size != None:
            config['inputs']['share']['vocab_size'] = int(vocab_size)
        if text1_corpus != None:
            config['inputs']['share']['text1_corpus'] = text1_corpus
        if text2_corpus != None:
            config['inputs']['share']['text2_corpus'] = text2_corpus
        if weights_file != None:
            config['global']['weights_file'] = weights_file
        if save_path != None:
            config['outputs']['predict']['save_path'] = save_path
        if text1_max_utt_num != None:
            config['inputs']['share']['text1_max_utt_num'] = int(text1_max_utt_num)
        if valid_batch_list != None:
            config['inputs']['valid']['batch_list'] = int(valid_batch_list)
        if test_batch_list != None:
            config['inputs']['test']['batch_list'] = int(test_batch_list)
        if predict_batch_list != None:
            config['inputs']['predict']['batch_list'] = int(predict_batch_list)
        if train_batch_size != None:
            config['inputs']['train']['batch_size'] = int(train_batch_size)
        if test_weights_iters != None:
            config['global']['test_weights_iters'] = int(test_weights_iters)

    if phase == 'train':
        train(config)
    elif phase == 'predict':
        predict(config)
    else:
        print 'Phase Error.'
    return

if __name__=='__main__':
    main(sys.argv)
