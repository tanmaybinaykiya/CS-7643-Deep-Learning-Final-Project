import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils import clip_grad_norm_
from torch.utils.data import DataLoader

from DataLoader import SquadDataset, collate_fn, obtain_glove_embeddings
from models import EncoderBILSTM, DecoderLSTM


def exp_lr_scheduler(optimizer, epoch, lr_decay=0.1, lr_decay_epoch=8):
    """Decay learning rate by a factor of lr_decay every lr_decay_epoch epochs"""
    if epoch < lr_decay_epoch:
        return optimizer

    for param_group in optimizer.param_groups:
        param_group['lr'] *= lr_decay
    return optimizer


def greedy_search(encoder, decoder, dev_loader, use_cuda, dev_idx_to_word_q, ):
    encoder.eval()
    decoder.eval()
    encoder.load_state_dict(torch.load("model_weights/encoder.pth"))
    decoder.load_state_dict(torch.load("model_weights/decoder.pth"))

    max_len = 30
    batch_size = 2
    for batch in dev_loader:

        questions, questions_org_len, answers, answers_org_len, pID = batch

        if use_cuda:
            questions = questions.cuda()
            answers = answers.cuda()

        encoder_input, encoder_len = answers, np.asarray(answers_org_len)
        decoder_input, decoder_len = questions, questions.shape[1]

        encoder_len = torch.LongTensor(encoder_len)
        if use_cuda:
            encoder_len = torch.LongTensor(encoder_len).cuda()
        encoder_out, encoder_hidden = encoder(encoder_input, torch.LongTensor(encoder_len))
        decoder_hidden = encoder_hidden
        # input to the first time step of decoder is <SOS> token.
        decoder_inp = torch.zeros((batch_size, 1), dtype=torch.long)
        seq_len = 0
        eval_mode = False
        predicted_sequences = []
        while seq_len < max_len:
            seq_len += 1
            decoder_out, decoder_hidden = decoder(decoder_inp, decoder_hidden, encoder_out,
                                                  torch.FloatTensor(answers_org_len), eval_mode=eval_mode)

            # obtaining log_softmax scores we need to minimize log softmax over a span.
            decoder_out = decoder_out.view(batch_size, -1)
            decoder_out = torch.nn.functional.log_softmax(decoder_out)
            prediction = torch.argmax(decoder_out, 1).unsqueeze(1)
            predicted_sequences.append(prediction)
            decoder_inp = prediction.clone()
            eval_mode = True
        given_sentence = [
            [dev_idx_to_word_q[str(answers[i][j].item())] for j in range(len(answers[i])) if answers[i][j] != 0]
            for i in range(len(answers))]
        ground_truth = [
            [dev_idx_to_word_q[str(questions[i][j].item())] for j in range(len(questions[i])) if questions[i][j] != 0]
            for i
            in range(len(questions))]
        predicted_sequences = [
            [dev_idx_to_word_q[str(predicted_sequences[j][i][0].item())] for j in range(len(predicted_sequences)) if
             dev_idx_to_word_q[str(predicted_sequences[j][i][0].item())] != '<EOS>'] for i in range(batch_size)]

        for sent, gt, pred in zip(given_sentence, ground_truth, predicted_sequences):
            print("Given: %s \n GT: %s \n Predicted Question: %s" % (sent, gt, pred))


def beam_search(encoder, decoder, dev_loader, dev_idx_to_word_q):
    batch_size = 2
    beam_span = 5
    # encoder.load_state_dict(torch.load("model_weights/encoder.pth"))
    # decoder.load_state_dict(torch.load("model_weights/decoder.pth"))
    encoder.eval()
    decoder.eval()
    for batch in dev_loader:
        questions, questions_org_len, answers, answers_org_len, pID = batch

        if torch.cuda.is_available():
            questions = questions.cuda()
            answers = answers.cuda()

        encoder_input, encoder_len = answers, np.asarray(answers_org_len)
        decoder_input, decoder_len = questions, questions.shape[1]
        encoder_len = torch.LongTensor(encoder_len)
        if torch.cuda.is_available():
            encoder_len = torch.LongTensor(encoder_len).cuda()
        encoder_out, encoder_hidden = encoder(encoder_input, torch.LongTensor(encoder_len))
        decoder_hidden = encoder_hidden

        # input to the first time step of decoder is <SOS> token.
        decoder_inp = torch.zeros((batch_size, 1), dtype=torch.long)
        final_scores = torch.zeros(batch_size, 25)
        final_indices = torch.zeros((batch_size, 25), dtype=torch.long)
        if torch.cuda.is_available():
            decoder_inp = decoder_inp.cuda()
            final_scores = final_indices.cuda()
            final_indices = final_indices.cuda()

        eval_mode = False
        parent_word = []
        actual_indices = []

        # list of all hidden states for a batch
        h_filler = []
        # list of all cell states for a batch
        c_filler = []

        # iterating through every word in the batch
        for word_index in range(decoder_len):

            # iterating through every beam_span proposal for ever word in the batch len(decoder_inp[0]) is the number of words each batch proposes.
            for j in range(len(decoder_inp[0])):

                # if the word is not the start word we have to pass in what the hidden and cell states for the LSTM would be
                if word_index > 0:
                    # find the hidden state and cell for the corresponding word proposed in the beam
                    h1 = [decoder_hidden_per_word[k][j] for k in range(batch_size)]
                    c1 = [cell_per_word[k][j] for k in range(batch_size)]

                    # reshape it to (N,B,Dim)
                    decoder_hidden = torch.stack(h1, 0).unsqueeze(0)
                    cell = torch.stack(c1, 0).unsqueeze(0)

                    # merge the hidden and state cells into a tuple
                    decoder_hidden = (decoder_hidden, cell)

                # passing the jth word predicted by the decoder in the previous step as input.
                decoder_out, dh = decoder(decoder_inp[:, j].view(-1, 1), decoder_hidden, encoder_out,
                                          torch.FloatTensor(answers_org_len), eval_mode=eval_mode)

                # obtaining log_softmax scores we need to minimize log softmax over a span.
                decoder_out = decoder_out.view(batch_size, -1)
                decoder_out = torch.nn.functional.log_softmax(decoder_out)

                # Minimizing log likelihood is equal to choosing the max of - log likelihood
                scores, indices = torch.topk(-decoder_out, k=beam_span, )

                # we need to save scores for all spans of the beam if beam span is 5 then every word in the batch can predict 5 words the next two arrays keep track
                # of scores and the indices, adding log likelihood scores is equivalent to multiplying probs.
                final_scores[:, j * beam_span:j * beam_span + beam_span] += scores
                final_indices[:, j * beam_span:j * beam_span + beam_span] = indices

                # we need to keep track of the hidden and cell states for each predicted word since they will be needed in the next time step.
                h_filler.append(dh[0])
                c_filler.append(dh[1])

            # find the 5 best scores and indices amongst all the top 5 predictions for each word
            scores, indices = torch.topk(final_scores, k=beam_span)

            # the baseline scores for the next 5 predictions is set here
            final_scores = scores.repeat(1, beam_span)

            # storing the parent word to track back in the future
            parent_word.append(indices.detach().cpu().numpy() // beam_span)

            # map the hidden and cell states of the best predictions across each word in the batch, note that the hidden and cell states
            # corresponding to highest are not constant across a batch
            decoder_hidden_per_word = np.asarray(
                [[h_filler[w.item() // beam_span][0][b] for w in indices[b]] for b in range(batch_size)])
            cell_per_word = np.asarray(
                [[c_filler[w.item() // beam_span][0][b] for w in indices[b]] for b in range(batch_size)])
            eval_mode = True

            # store the actual indices of the 5 best words.
            indices = final_indices.gather(1, indices)
            # store the indices for future reference
            actual_indices.append(indices.detach().cpu().numpy())
            # Input to next time step are the 5 best words predicted by each word in the current batch so we will have batch*beam size number of inputs
            decoder_inp = indices.clone()
            h_filler = []
            c_filler = []

        actual_indices = np.asarray(actual_indices)
        parent_word = np.asarray(parent_word)

        # find the index of the highest score
        highest_score_pos = torch.argmax(scores, 1)

        # iterate from last word to first word across all batches using the stored indices and parent nd array.
        prediction = []
        for i in range(len(actual_indices) - 1, -1, -1):
            # actual_indices[i] corresponds to the predictions in the ith time step, second index refers to each batch
            prediction.append(actual_indices[i, np.arange(batch_size), highest_score_pos])
            highest_score_pos = parent_word[i, np.arange(batch_size), highest_score_pos]

        # reverse the prediction to output from start to end
        prediction = prediction[::-1]
        print(prediction)
        for i in range(len(prediction)):
            print(dev_idx_to_word_q[str(prediction[i][0])])


def train(encoder, decoder, num_epoch, batch_per_epoch, train_iter, criterion, optimizer_enc, optimizer_dec, is_cuda):
    losses = []
    for eachEpoch in range(num_epoch):
        total_batch_loss = 0
        for eachBatch in range(batch_per_epoch):
            # i = batch_per_epoch * eachEpoch + eachBatch + 1  # global step
            batch = next(train_iter)

            # each batch is size 1 for now
            questions, questions_org_len, answers, answers_org_len, pID = batch

            if is_cuda:
                questions = questions.cuda()
                answers = answers.cuda()

            encoder_input, encoder_len = answers, answers_org_len
            decoder_input, decoder_len = questions, questions.shape[1]

            if is_cuda:
                encoder_out, encoder_hidden = encoder(encoder_input, torch.LongTensor(encoder_len).cuda())
            else:
                encoder_out, encoder_hidden = encoder(encoder_input, torch.LongTensor(encoder_len))
            decoder_out, decoder_hidden = decoder(decoder_input[:, :-1], encoder_hidden, encoder_out,
                                                  torch.FloatTensor(answers_org_len))

            decoder_out = decoder_out.transpose(0, 1).contiguous()
            decoder_out = decoder_out.transpose(1, 2).contiguous()
            loss = criterion(decoder_out, questions[:, :-1])
            optimizer_enc.zero_grad()
            optimizer_dec.zero_grad()
            loss.backward()
            clip_grad_norm_(encoder.parameters(), 5)
            clip_grad_norm_(decoder.parameters(), 5)
            optimizer_enc.step()
            optimizer_dec.step()
            total_batch_loss += loss.item()
            break
        losses.append(total_batch_loss)
        print("Loss for the batch is")
        print(total_batch_loss / batch_per_epoch)

    torch.save(encoder.state_dict(), "model_weights/encoder.pth")
    torch.save(decoder.state_dict(), "model_weights/decoder.pth")
    return losses


def main(use_cuda=False, filename_glove='data/glove.840B.300d.txt'):
    use_cuda = use_cuda and torch.cuda.is_available()

    train_dataset = SquadDataset(split="train")
    idx_to_word_sent = train_dataset.get_answer_idx_to_word()
    word_to_idx_sent = train_dataset.get_answer_word_to_idx()
    idx_to_word_q = train_dataset.get_question_idx_to_word()
    word_to_idx_q = train_dataset.get_question_idx_to_word()

    train_vocab_size_sent = len(word_to_idx_sent)
    train_vocab_size_q = len(word_to_idx_q)
    num_epoch = 15
    batch_size = 2
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=0, collate_fn=collate_fn,
                              pin_memory=True)

    train_iter = iter(train_loader)

    word_embeddings_glove_q = obtain_glove_embeddings(filename_glove, word_to_idx_q,
                                                      pruned_glove_filename="data/question_glove_embeddings.npy")
    word_embeddings_glove_sent = obtain_glove_embeddings(filename_glove, word_to_idx_sent,
                                                         pruned_glove_filename="data/answer_glove_embeddings.npy")

    encoder = EncoderBILSTM(vocab_size=train_vocab_size_sent, n_layers=2, embedding_dim=300, hidden_dim=500,
                            dropout=0, embeddings=word_embeddings_glove_sent)
    decoder = DecoderLSTM(vocab_size=train_vocab_size_q, embedding_dim=300, hidden_dim=500, n_layers=1,
                          encoder_hidden_dim=500, embeddings=word_embeddings_glove_q)

    if torch.cuda.is_available() and use_cuda:
        encoder = encoder.cuda()
        decoder = decoder.cuda()

    n_train = len(train_loader)
    batch_per_epoch = n_train // batch_size

    criterion = nn.CrossEntropyLoss(ignore_index=0)
    optimizer_enc = torch.optim.SGD(encoder.parameters(), lr=1.0)
    optimizer_dec = torch.optim.SGD(decoder.parameters(), lr=1.0)
    train(encoder, decoder, num_epoch, batch_per_epoch, train_iter, criterion, optimizer_enc, optimizer_dec, use_cuda)

    dev_dataset = SquadDataset(split="dev")

    dev_loader = DataLoader(
        dev_dataset, batch_size=batch_size, shuffle=True, num_workers=0, collate_fn=collate_fn, pin_memory=True)


if __name__ == '__main__':
    main()
