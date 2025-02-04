import os
from typing import Any
import numpy as np
from pytorch_lightning.utilities.types import STEP_OUTPUT
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import pytorch_lightning as L
from sklearn.metrics import mean_absolute_error, roc_auc_score
from scipy.stats import pearsonr
from models.func import kld, mmd, reconAcc_pearsonCorr, reconAcc_relativeError, mse, crossEntropy, bce, init_weights
from models.clustering import *
import pandas as pd
import matplotlib.pyplot as plt


class XVAE_w_advNet_pingpong(L.LightningModule):
    def __init__(self, 
                 PATH_xvae_ckpt,
                 PATH_advNet_ckpt,
                 labels_onehot,
                 lamdba_deconf = 1,
                 distance="mmd", 
                 beta=1): 
        super().__init__()
        self.labels_onehot = labels_onehot
        self.lamdba_deconf = lamdba_deconf
        self.distance = distance
        self.beta = beta
        self.save_hyperparameters()
        self.test_step_outputs = [] 

        ### Load pre-trained XVAE model
        self.xvae_pre = XVAE_preTrg.load_from_checkpoint(PATH_xvae_ckpt)

        ### Load pre-trained advNet and freeze weights
        self.advNet_pre = advNet.load_from_checkpoint(PATH_advNet_ckpt)

    def configure_optimizers(self):
        optimizer = optim.Adam(filter(lambda p: p.requires_grad, self.parameters()), lr=1e-3, betas=(0.9, 0.999), eps=1e-08, amsgrad=False, weight_decay=0)
        # Using a scheduler is optional but can be helpful.
        # The scheduler reduces the LR if the validation performance hasn't improved for the last N epochs
        scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="min", factor=0.2, patience=20, min_lr=5e-5)
        return {"optimizer": optimizer, "lr_scheduler": scheduler, "monitor": "val_combined_loss"}

    def compute_loss_combined(self, batch):
        ''' 
        Autoencoder loss 
        '''
        recon_loss, reg_loss, _ = self.xvae_pre.compute_loss(batch)
        ae_loss = recon_loss + reg_loss

        ''' 
        Adversarial net loss
        ''' 
        regr_loss, clf_loss = self.advNet_pre.compute_loss(batch)
        advNet_loss = regr_loss + clf_loss 

        '''
        Combined loss function for adversarial training
        (combined loss) = (autoencoder loss) - lambda * (adversarial loss)
        '''
        combined_loss = ae_loss - self.lamdba_deconf * advNet_loss

        return ae_loss, advNet_loss, combined_loss
    

    def compute_loss_advNet(self, batch):
        ''' 
        Adversarial net loss
        ''' 
        regr_loss, clf_loss = self.advNet_pre.compute_loss(batch)
        advNet_loss = regr_loss + clf_loss 
        return advNet_loss

    ### Ping pong training
    def training_step(self, batch, batch_idx):
        if self.current_epoch % 2 == 0:
            ## Only train on 1 batch (first one is random as dataset is shuffled each epoch)
            if batch_idx == 1:
                for param in self.xvae_pre.parameters():
                    param.requires_grad = True
                for param in self.advNet_pre.parameters():
                    param.requires_grad = False        
                # Calculate loss
                ae_loss, advNet_loss, combined_loss = self.compute_loss_combined(batch)
                # Save metric 
                self.log('train_ae_loss', ae_loss, on_step = False, on_epoch = True, prog_bar = True)       
                self.log('train_combined_loss', combined_loss, on_step = False, on_epoch = True, prog_bar = True)
                # Prepare for next epoch
                return combined_loss
        else:             
            ## Train on all batches
            # Freeze that fucker
            for param in self.xvae_pre.parameters():
                param.requires_grad = False
            for param in self.advNet_pre.parameters():
                param.requires_grad = True        
            advNet_loss = self.compute_loss_advNet(batch)
            # save metric
            self.log('train_advNet_loss', advNet_loss, on_step = False, on_epoch = True, prog_bar = True)
            return advNet_loss


    def validation_step(self, batch, batch_idx):
        ae_loss, advNet_loss, combined_loss = self.compute_loss_combined(batch)
        self.log('val_ae_loss', ae_loss, on_step = False, on_epoch = True, prog_bar = True)       
        self.log('val_advNet_loss', advNet_loss, on_step = False, on_epoch = True, prog_bar = True)
        self.log('val_combined_loss', combined_loss, on_step = False, on_epoch = True, prog_bar = True)
        return combined_loss
    
    def test_step(self, batch, batch_idx):
        ''' Do a final quality check once using the external test set; Add here our other QC metrices; Relative Error, R2, clustering metric '''
        batch, y = batch[:3], batch[-1]
        ae_loss, advNet_loss, combined_loss = self.compute_loss_combined(batch)
        x_hat = self.xvae_pre.forward(*batch[:2])
        z = self.xvae_pre.generate_embedding(*batch[:2])
        cov_pred = self.xvae_pre.forward(*batch[:2])

        self.log('test_combined_loss', combined_loss , on_step = False, on_epoch = True)
        self.test_step_outputs.append({"z": z, "y": y,   ### clustering
                                       "recon": x_hat, "x": batch[:2],    ### reconstruction
                                       "conf_pred": cov_pred, "conf": batch[2]})      ### covariate prediction

    def on_test_epoch_end(self):
        ''' Clustering '''
        LF = torch.cat([x["z"] for x in self.test_step_outputs], 0)
        Y = torch.cat([x["y"] for x in self.test_step_outputs], 0)
        LF = LF.detach().cpu().numpy() # convert (GPU or CPU) tensor to numpy for the clustering
        Y = Y.detach().cpu().numpy()
        clust = kmeans(LF, self.xvae_pre.c)
        SS, DB = internal_metrics(LF, clust)
        ARI, NMI = external_metrics(clust, Y)

        ''' Reconstruction accuracy (Pearson correlation between reconstruction and input) '''
        x1 = torch.cat([x["x"][0] for x in self.test_step_outputs], 0).detach().cpu().numpy() 
        x2 = torch.cat([x["x"][1] for x in self.test_step_outputs], 0).detach().cpu().numpy() 
        x1_hat = torch.cat([x["recon"][0] for x in self.test_step_outputs], 0).detach().cpu().numpy() 
        x2_hat = torch.cat([x["recon"][1] for x in self.test_step_outputs], 0).detach().cpu().numpy() 
        reconAcc_x1, reconAcc_x2 = reconAcc_pearsonCorr(x1, x1_hat, x2, x2_hat)        

        ''' Relative Error using L2 norm '''
        relativeError = reconAcc_relativeError(x1, x1_hat,  x2, x2_hat)

        ''' Absolute correlation to confounding variables '''
        conf = torch.cat([x["conf"] for x in self.test_step_outputs], 0).detach().cpu().numpy() 
        corr_conf = [np.abs(np.corrcoef(LF.T, conf[:,i].T)[:-1,-1]) for i in range(conf.shape[1])]
        fig, ax = plt.subplots(figsize=(15,5))
        im = plt.imshow(corr_conf, cmap='hot', interpolation='nearest', vmin=0, vmax=0.5)
        ax.set_yticks(np.arange(conf.shape[1]), labels=self.labels_onehot)
        ax.tick_params(axis='both', labelsize=10)
        plt.colorbar(im)
        self.logger.experiment.add_figure(tag="Correlation with covariates", figure=fig)

        ''' 
        Association between clustering and confounders 
        '''
        pvals = test_confounding(clust, conf)

        ''' Summary Table for tensorboard'''
        table = f"""
            | Metric | Value  |
            |----------|-----------|
            | Silhouette score    | {SS:.2f} |
            | DB index    | {DB:.2f} |
            | Adjusted Rand Index   | {ARI:.2f} |
            | Normalized Mutual Info   | {NMI:.2f} |
            | Reconstruction accuracy X1 - Pearson correlation (mean+-std)   | {np.mean(reconAcc_x1):.2f}+-{np.std(reconAcc_x1):.2f} |
            | Reconstruction accuracy X2 - Pearson correlation (mean+-std)   | {np.mean(reconAcc_x2):.2f}+-{np.std(reconAcc_x2):.2f} |
            | Reconstruction accuracy - Relative error (L2 norm)   | {relativeError:.2f} |                                    
        """
        table = '\n'.join(l.strip() for l in table.splitlines())
        for i in range(conf.shape[1]):#):
            table += f"| Association with {self.labels_onehot[i]}  | {pvals[i]:.2e} |\n"
        self.logger.experiment.add_text("Results on test set", table,0)

        ''' Visualise embedding '''        
        ### conf_list = [conf[:,i] for i in range(conf.shape[1])]   ## There is an option to give multiple labels but I couldn't make it work
        self.logger.experiment.add_embedding(LF, metadata=Y)
        return


class advNet(L.LightningModule):
    def __init__(self,
                 PATH_xvae_ckpt,
                 ls,
                 num_cov_regr,
                 num_cov_clf,
                 loss_func_regr="mse"):
        super().__init__()
        self.ls = ls
        self.num_cov_regr = num_cov_regr
        self.num_cov_clf = num_cov_clf
        self.loss_func_regr = loss_func_regr
        self.test_step_outputs = []     # accumulate latent factors for all samples in every test step
        self.save_hyperparameters()

        ### Load pre-trained XVAE model
        self.xvae = XVAE_preTrg.load_from_checkpoint(PATH_xvae_ckpt)
        self.xvae.freeze()

        ### adversarial net
        self.adv_net_hidden = nn.Sequential(nn.Linear(self.ls, 10),
                                     nn.LeakyReLU())
        
        if self.num_cov_regr: 
            self.adv_net_regr = nn.Sequential(nn.Linear(10, self.num_cov_regr),
                                              nn.ReLU())
            for ele in [self.adv_net_regr]:
                ele.apply(lambda m: init_weights(m, "rai"))

        if self.num_cov_clf:
            if self.num_cov_clf > 1:       
                self.adv_net_clf = nn.Sequential(nn.Linear(10,  self.num_cov_clf),
                                                nn.Softmax())     
                self.loss_func_clf = "crossEntropy"
            else: 
                self.adv_net_clf = nn.Sequential(nn.Linear(10,  self.num_cov_clf),
                                                nn.Sigmoid())     
                self.loss_func_clf = "bce"
            for ele in [self.adv_net_clf]:
                ele.apply(lambda m: init_weights(m, "rai"))

        ### Initialise weights  
        for ele in [self.adv_net_hidden]:
            ele.apply(lambda m: init_weights(m, "rai"))

    def forward(self, x1, x2):
        z = self.xvae.generate_embedding(x1, x2)
        hidden = self.adv_net_hidden(z)
        if self.num_cov_regr: 
            y_pred_regr = self.adv_net_regr(hidden)
        else: y_pred_regr = torch.nan
        if self.num_cov_clf:
            y_pred_clf = self.adv_net_clf(hidden)
        else: y_pred_clf = torch.nan
        return y_pred_regr, y_pred_clf

    def configure_optimizers(self):
        optimizer = optim.Adam(self.parameters(), lr=5e-4, betas=(0.9, 0.999), eps=1e-08, amsgrad=False, weight_decay=0.001)
        # Using a scheduler is optional but can be helpful.
        # The scheduler reduces the LR if the validation performance hasn't improved for the last N epochs
        scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="min", factor=0.2, patience=20, min_lr=5e-5)
        return {"optimizer": optimizer, "lr_scheduler": scheduler, "monitor": "advNet_val_loss"}


    def compute_loss(self, batch):
        x1, x2, cov = batch 
        y_pred_regr, y_pred_clf = self.forward(x1,x2)

        ### MSE loss for regression (age)
        if self.loss_func_regr == "mse" and self.num_cov_regr:
            cov_regr = cov[:,:self.num_cov_regr]
            cov_regr = cov_regr.to(torch.float32)
            regrloss = mse(y_pred_regr, cov_regr.flatten()) 
        else: regrloss = 0

        ### BSE loss for classification (OHE)
        if self.num_cov_clf:
            cov_clf = cov[:,self.num_cov_regr:self.num_cov_clf+1]
            cov_clf = cov_clf.to(torch.float32)
            if self.loss_func_clf == "crossEntropy":
                clfloss = crossEntropy(y_pred_clf, cov_clf)    
            else:
                clfloss = bce(y_pred_clf, cov_clf)              
        else: clfloss = 0        
        return regrloss, clfloss

    def training_step(self, batch, batch_idx):
        x1, x2, cov = batch 
        regr_loss, clf_loss = self.compute_loss(batch)
        loss = regr_loss + clf_loss
        self.log('advNet_train_loss', loss, on_step = False, on_epoch = True, prog_bar = True)     
        return loss
    
    def validation_step(self, batch, batch_idx):
        x1, x2, cov = batch 
        regr_loss, clf_loss = self.compute_loss(batch)
        loss = regr_loss + clf_loss  
        self.log('advNet_val_loss', loss, on_step = False, on_epoch = True)            
        return loss
    
    def test_step(self, batch, batch_idx):
        batch, y = batch[:3], batch[-1]

        regr_loss, clf_loss = self.compute_loss(batch)
        loss = regr_loss + clf_loss  

        y_pred_regr, y_pred_clf = self.forward(batch[0],batch[1])
        self.log('advNet_test_loss', loss, on_step = False, on_epoch = True)

        ### gather all results to hand to final test
        dic_test_epoch_end = dict()
        dic_test_epoch_end["cov"] = batch[2]
        if self.num_cov_regr:
            dic_test_epoch_end["cov_pred_regr"] = y_pred_regr
        if self.num_cov_clf:
            dic_test_epoch_end["cov_pred_clf"] = y_pred_clf
        self.test_step_outputs.append(dic_test_epoch_end)
        
    def on_test_epoch_end(self):
        cov = torch.cat([x["cov"] for x in self.test_step_outputs], 0).detach().cpu().numpy()
        try: 
            y_regr = torch.cat([x["cov_pred_regr"] for x in self.test_step_outputs], 0).detach().cpu().numpy() 
        except KeyError: pass
        try:
            y_clf = torch.cat([x["cov_pred_clf"] for x in self.test_step_outputs], 0).detach().cpu().numpy() 
        except KeyError: pass

        ''' Score regression: '''
        if self.num_cov_regr:
            mae_regr = mean_absolute_error(cov[:,:self.num_cov_regr], y_regr)
            pearson_regr = pearsonr(cov[:,:self.num_cov_regr].flatten(), y_regr.flatten())[0]
        else: mae_regr = pearson_regr = np.nan

        ''' Score classification: '''
        if self.num_cov_clf:
            auc = roc_auc_score(cov[:,self.num_cov_regr:self.num_cov_clf+1], y_clf)
        else: 
            auc = np.nan

        ''' Summary Table for tensorboard'''
        table = f"""
            | Metric | Value  |
            |----------|-----------|
            | MAE    | {mae_regr:.2f} |
            | Pearson correlation    | {pearson_regr:.2f} |
            | AUC    | {auc:.2f} |
        """
        table = '\n'.join(l.strip() for l in table.splitlines())
        self.logger.experiment.add_text("Pre-training adversarial net (test set)", table,0)
        return
    

class XVAE_preTrg(L.LightningModule):
    def __init__(self, 
                 x1_size, 
                 x2_size, 
                 ls, 
                 distance, 
                 beta=1): 

        super().__init__()
        self.ls = ls                    # latent size
        self.distance = distance        # regularisation used
        self.beta = beta                # weight for distance term in loss function
        self.c = 6                      # number of clusters
        self.test_step_outputs = []     # accumulate latent factors for all samples in every test step
        self.save_hyperparameters()

        ### encoder
        ### NOTE: hard coded reduction for now - change later!!
        self.encoder_x1_fc = nn.Sequential(nn.Linear(x1_size, 128), 
                                           nn.LeakyReLU(), 
                                           nn.BatchNorm1d(128))   
        self.encoder_x2_fc = nn.Sequential(nn.Linear(x2_size, 128), 
                                           nn.LeakyReLU(), 
                                           nn.BatchNorm1d(128))   
        ### fusing
        self.encoder_fuse = nn.Sequential(nn.Linear(128+128,     
                                                    128), 
                                          nn.LeakyReLU(), 
                                          nn.BatchNorm1d(128))  
        
        ### latent embedding
        self.embed_mu = nn.Linear(128, self.ls)
        self.embed_log_var = nn.Linear(128, self.ls)

        ### decoder
        self.decoder_sample = nn.Sequential(nn.Linear(self.ls, 128),
                                            nn.LeakyReLU())
        self.decoder_x1_fc = nn.Sequential(nn.Linear(128, x1_size),
                                           nn.Sigmoid())
        self.decoder_x2_fc = nn.Sequential(nn.Linear(128, x2_size),
                                           nn.Sigmoid())

        ### Initialise weights
        for ele in [self.encoder_x1_fc, self.encoder_x2_fc, self.encoder_fuse, self.embed_mu, self.embed_log_var, self.decoder_sample, self.decoder_x1_fc, self.decoder_x2_fc]:
            ele.apply(lambda m: init_weights(m, "rai"))


    def sample_z(self, mu, log_var):
        # Reparametrization Trick to allow gradients to backpropagate from the 
        #stochastic part of the model
        sigma = torch.exp(0.5*log_var)
        z = torch.randn(size = (mu.size(0), mu.size(1)))
        z = z.type_as(mu) # Setting z to be .cuda when using GPU training 
        return mu + sigma*z


    def encode(self, x1, x2):
        x1 = self.encoder_x1_fc(x1)
        x2 = self.encoder_x2_fc(x2)
        x_fused = torch.cat((x1, x2), dim=1)
        x_hidden = self.encoder_fuse(x_fused)
        mu = self.embed_mu(x_hidden)
        log_var = self.embed_log_var(x_hidden)
        return mu, log_var


    def decode(self, z):
        x_fused_hat = self.decoder_sample(z)
        x1_hat = self.decoder_x1_fc(x_fused_hat)
        x2_hat = self.decoder_x2_fc(x_fused_hat)
        return x1_hat, x2_hat


    def forward(self, x1, x2):
        mu, log_var = self.encode(x1, x2)
        z = self.sample_z(mu, log_var)
        x1_hat, x2_hat = self.decode(z)
        return x1_hat, x2_hat        
    

    def generate_embedding(self, x1, x2):
        mu, log_var = self.encode(x1, x2)
        z = self.sample_z(mu, log_var)
        return z


    def configure_optimizers(self):
        optimizer = optim.Adam(self.parameters(), lr=1e-3, betas=(0.9, 0.999), eps=1e-08, amsgrad=False, weight_decay=0)
        # Using a scheduler is optional but can be helpful.
        # The scheduler reduces the LR if the validation performance hasn't improved for the last N epochs
        scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="min", factor=0.2, patience=20, min_lr=5e-5)
        return {"optimizer": optimizer, "lr_scheduler": scheduler, "monitor": "val_loss"}


    def compute_loss(self, batch):
        x1, x2, _ = batch
        mu, log_var = self.encode(x1, x2)
        z = self.sample_z(mu, log_var)
        x1_hat, x2_hat = self.decode(z)

        if self.distance == "mmd":
            true_samples = torch.randn([x1.shape[0], self.ls], device=z.device)
            distance = mmd(true_samples, z)
        if self.distance == "kld":
            distance = kld(mu, log_var)         

        recon_loss_criterion = nn.MSELoss(reduction="mean")  ##### CHECK "mean" here again! "sum" better?
        recon_loss_x1 = recon_loss_criterion(x1, x1_hat)
        recon_loss_x2 = recon_loss_criterion(x2, x2_hat)
        recon_loss = recon_loss_x1 + recon_loss_x2
        
        reg_loss = self.beta * distance

        return recon_loss, reg_loss, z


    def training_step(self, batch, batch_idx):
        recon_loss, reg_loss, z = self.compute_loss(batch)
        loss = recon_loss + reg_loss
        self.log('train_recon_loss', recon_loss, on_step = False, on_epoch = True, prog_bar = True)       
        self.log('train_reg_loss', reg_loss, on_step = False, on_epoch = True, prog_bar = True)
        self.log('train_loss', loss, on_step = True, on_epoch = True, prog_bar = True)
        return loss


    def validation_step(self, batch, batch_idx):
        recon_loss, reg_loss, z = self.compute_loss(batch)
        loss = recon_loss + reg_loss
        self.log('val_loss', loss, on_step = False, on_epoch = True)
        return loss


    def test_step(self, batch, batch_idx):
        ''' Do a final quality check once using the external test set; Add here our other QC metrices; Relative Error, R2, clustering metric '''
        batch, y = batch[:3], batch[-1]
        recon_loss, reg_loss, z = self.compute_loss(batch)
        loss = recon_loss + reg_loss
        x_hat = self.forward(*batch[:2])

        self.log('test_loss', loss , on_step = False, on_epoch = True)
        self.test_step_outputs.append({"z": z, "y": y, "recon": x_hat, "x": batch[:2]})
        return loss
    

    def on_test_epoch_end(self):
        '''
        Quality checks on Test set: 
            - Clustering:
                - ... 
        '''

        ''' Clustering '''
        LF = torch.cat([x["z"] for x in self.test_step_outputs], 0)
        Y = torch.cat([x["y"] for x in self.test_step_outputs], 0)
        LF = LF.detach().cpu().numpy() # convert (GPU or CPU) tensor to numpy for the clustering
        Y = Y.detach().cpu().numpy()
        clust = kmeans(LF, self.c)
        SS, DB = internal_metrics(LF, clust)
        ARI, NMI = external_metrics(clust, Y)

        ''' Reconstruction accuracy (Pearson correlation between reconstruction and input) '''
        x1 = torch.cat([x["x"][0] for x in self.test_step_outputs], 0).detach().cpu().numpy() 
        x2 = torch.cat([x["x"][1] for x in self.test_step_outputs], 0).detach().cpu().numpy() 
        x1_hat = torch.cat([x["recon"][0] for x in self.test_step_outputs], 0).detach().cpu().numpy() 
        x2_hat = torch.cat([x["recon"][1] for x in self.test_step_outputs], 0).detach().cpu().numpy() 
        reconAcc_x1, reconAcc_x2 = reconAcc_pearsonCorr(x1, x1_hat, x2, x2_hat)

        ''' Relative Error using L2 norm '''
        relativeError = reconAcc_relativeError(x1, x1_hat,  x2, x2_hat)


        ''' Summary Table for tensorboard'''
        table = f"""
            | Metric | Value  |
            |----------|-----------|
            | Silhouette score    | {SS:.2f} |
            | DB index    | {DB:.2f} |
            | Adjusted Rand Index   | {ARI:.2f} |
            | Normalized Mutual Info   | {NMI:.2f} |
            | Reconstruction accuracy X1 - Pearson correlation (mean+-std)   | {np.mean(reconAcc_x1):.2f}+-{np.std(reconAcc_x1):.2f} |
            | Reconstruction accuracy X2 - Pearson correlation (mean+-std)   | {np.mean(reconAcc_x2):.2f}+-{np.std(reconAcc_x2):.2f} |
            | Reconstruction accuracy - Relative error (L2 norm)   | {relativeError:.2f} |                                    
        """
        table = '\n'.join(l.strip() for l in table.splitlines())
        self.logger.experiment.add_text("Results on test set", table,0)

        ''' Visualise embedding '''
        self.logger.experiment.add_embedding(LF, metadata=Y)

        return 







