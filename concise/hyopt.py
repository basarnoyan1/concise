"""Train the models
"""
from keras.callbacks import EarlyStopping, History, TensorBoard
from keras.models import load_model
import hyperopt
from hyperopt.utils import coarse_utcnow
from hyperopt.mongoexp import MongoTrials
import concise.eval_metrics as ce
from concise.utils.helper import write_json, merge_dicts
from concise.utils.model_data import (subset, split_train_test_idx, split_KFold_idx)
from datetime import datetime, timedelta
from uuid import uuid4
from hyperopt import STATUS_OK
import numpy as np
import pandas as pd
from copy import deepcopy
import os
import glob
import pprint
import logging

logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# TODO - have a system-wide config for this
DEFAULT_IP = "ouga03"
DEFAULT_SAVE_DIR = "/s/project/deepcis/hyperopt/"

# TODO - think about the workflow
# def oof_predictions(fn, params, epochs, model_name, cache_model=True, save_model=True):
#     model_dir = fn.save_dir_exp + "/eval_models/"
#     model_path = model_dir + "/{0}.h5".format(model_name)
#     eval_metrics_path = model_dir + "/{0}_eval_metrics.json".format(model_name)

#     train, test = get_data(fn.data_fn, params)

#     if cache_model and os.path.isfile(model_path):
#         model = load_model(model_path)
#     else:
#         model = get_model(fn.model_fn, train, params)
#         model.train(train[0], train[1],
#                     batch_size=params["fit"]["batch_size"],
#                     epochs=epochs)

#     y_pred = model.predict(test[0])
#     eval_metrics = eval_model(model, test, fn.add_eval_metrics)

#     if save_model:
#         os.makedirs(model_dir, exist_ok=True)
#         # TODO - how to name things
#         model.save(model_path)

#     #write_json(eval_metrics, eval_metrics_path)

#     return eval_metrics, {"y_pred": y_pred, "y_true": test[1]}


def test_fn(fn, hyper_params, n_train=1000, tmp_dir="/tmp/concise_hyopt_test/"):
    """Test the correctness of the function before executing on large scale
    1. Run without error
    2. Correct save/load model to disk

    # Arguments

        n_train: int, number of training points
    """
    def wrap_data_fn(data_fn, n_train=100):
        def new_data_fn(*args, **kwargs):
            train, test = data_fn(*args, **kwargs)
            train = subset(train, np.arange(n_train))
            return train, test
        return new_data_fn
    start_time = datetime.now()
    fn = deepcopy(fn)
    hyper_params = deepcopy(hyper_params)
    fn.save_dir = tmp_dir
    fn.save_model = True
    fn.data_fn = wrap_data_fn(fn.data_fn, n_train)

    # sample from hyper_params
    param = hyperopt.pyll.stochastic.sample(hyper_params)
    # overwrite the number of epochs
    if param.get("fit") is None:
        param["fit"] = {}
    param["fit"]["epochs"] = 1

    # correct execution
    res = fn(param)
    print("Returned value:")
    pprint.pprint(res)
    assert res["status"] == STATUS_OK

    # correct model loading
    model_path = max(glob.iglob(fn.save_dir_exp + '/train_models/*.h5'),
                     key=os.path.getctime)
    assert datetime.fromtimestamp(os.path.getctime(model_path)) > start_time
    load_model(model_path)


class CMongoTrials(MongoTrials):

    def __init__(self, db_name, exp_name,
                 ip=DEFAULT_IP, port=1234, kill_timeout=None, **kwargs):
        """
        Concise Mongo trials. Extends MonoTrials with the following four methods:

        - get_trial
        - best_trial_tid
        - optimal_epochs
        - overrides: count_by_state_unsynced
        - delete_running
        - valid_tid
        - train_history
        - get_ok_results
        - as_df

        kill_timeout, int: Maximum runtime of a job (in seconds) before it gets killed. None for infinite.
        """
        self.kill_timeout = kill_timeout
        if self.kill_timeout is not None and self.kill_timeout < 60:
            logger.warning("kill_timeout < 60 -> Very short time for " +
                           "each job to complete before it gets killed!")

        super(CMongoTrials, self).__init__(
            'mongo://{ip}:{p}/{n}/jobs'.format(ip=ip, p=port, n=db_name), exp_key=exp_name, **kwargs)

    def get_trial(self, tid):
        """Retrieve trial by tid
        """
        lid = np.where(np.array(self.tids) == tid)[0][0]
        return self.trials[lid]

    def get_param(self, tid):
        return self.get_trial(tid)["result"]["param"]

    def best_trial_tid(self, rank=0):
        """Get tid of the best trial

        rank=0 means the best model
        rank=1 means second best
        ...
        """
        candidates = [t for t in self.trials
                      if t['result']['status'] == STATUS_OK]
        losses = [float(t['result']['loss']) for t in candidates]
        assert not np.any(np.isnan(losses))
        lid = np.where(np.argsort(losses).argsort() == rank)[0][0]
        return candidates[lid]["tid"]

    def optimal_epochs(self, tid):
        trial = self.get_trial(tid)
        patience = trial["result"]["param"]["fit"]["patience"]
        epochs = trial["result"]["param"]["fit"]["epochs"]

        def optimal_len(hist):
            c_epoch = max(hist["loss"]["epoch"]) + 1
            if c_epoch == epochs:
                return epochs
            else:
                return c_epoch - patience

        hist = trial["result"]["history"]
        if isinstance(hist, list):
            return int(np.floor(np.array([optimal_len(h) for h in hist]).mean()))
        else:
            return optimal_len(hist)

    # def refresh(self):
    #     """Extends the original object
    #     """
    #     self.refresh_tids(None)
    #     if self.kill_timeout is not None:
    #         # TODO - remove dry_run
    #         self.delete_running(self.kill_timeout, dry_run=True)

    def count_by_state_unsynced(self, arg):
        """Extends the original object in order to inject checking
        for stalled jobs and killing them if they are running for too long
        """
        if self.kill_timeout is not None:
            self.delete_running(self.kill_timeout)
        return super(CMongoTrials, self).count_by_state_unsynced(arg)

    def delete_running(self, timeout_last_refresh=0, dry_run=False):
        """Delete jobs stalled in the running state for too long

        timeout_last_refresh, int: number of seconds
        """
        running_all = self.handle.jobs_running()
        running_timeout = [job for job in running_all
                           if coarse_utcnow() > job["refresh_time"] +
                           timedelta(seconds=timeout_last_refresh)]
        if len(running_timeout) == 0:
            # Nothing to stop
            self.refresh_tids(None)
            return None

        if dry_run:
            logger.warning("Dry run. Not removing anything.")

        logger.info("Removing {0}/{1} running jobs. # all jobs: {2} ".
                    format(len(running_timeout), len(running_all), len(self)))

        now = coarse_utcnow()
        logger.info("Current utc time: {0}".format(now))
        logger.info("Time horizont: {0}".format(now - timedelta(seconds=timeout_last_refresh)))
        for job in running_timeout:
            logger.info("Removing job: ")
            pjob = job.to_dict()
            del pjob["misc"]  # ignore misc when printing
            logger.info(pprint.pformat(pjob))
            if not dry_run:
                self.handle.delete(job)
                logger.info("Job deleted")
        self.refresh_tids(None)

    def valid_tid(self):
        """List all valid tid's
        """
        return [t["tid"] for t in self.trials if t["result"]["status"] == "ok"]

    def train_history(self, tid=None):
        """Get train history as pd.DataFrame
        """

        def result2history(result):
            if isinstance(result["history"], list):
                return pd.concat([pd.DataFrame(hist["loss"]).assign(fold=i)
                                  for i, hist in enumerate(result["history"])])
            else:
                return pd.DataFrame(result["history"]["loss"])

        # use all
        if tid is None:
            tid = self.valid_tid()

        res = [result2history(t["result"]).assign(tid=t["tid"]) for t in self.trials
               if t["tid"] in _listify(tid)]
        df = pd.concat(res)

        # reorder columns
        fold_name = ["fold"] if "fold" in df else []
        df = _put_first(df, ["tid"] + fold_name + ["epoch"])
        return df

    def get_ok_results(self, verbose=True):
        """Return a list of results with ok status
        """
        not_ok = np.where(np.array(self.statuses()) != "ok")[0]

        if len(not_ok) > 0 and verbose:
            print("{0}/{1} trials were not ok.".format(len(not_ok), len(self.trials)))
            print("Trials: " + str(not_ok))
            print("Statuses: " + str(np.array(self.statuses())[not_ok]))

        r = [merge_dicts({"tid": t["tid"]}, t["result"].to_dict()) for t in self.trials if t["result"]["status"] == "ok"]
        return r

    def as_df(self, ignore_vals=["history"], separator=".", verbose=True):
        """Return a pd.DataFrame view of the whole experiment
        """

        def add_eval(res):
            if "eval" not in res:
                if isinstance(res["history"], list):
                    # take the average across all folds
                    eval_names = list(res["history"][0]["loss"].keys())
                    eval_metrics = np.array([[v[-1] for k, v in hist["loss"].items()]
                                             for hist in res["history"]]).mean(axis=0).tolist()
                    res["eval"] = {eval_names[i]: eval_metrics[i] for i in range(len(eval_metrics))}
                else:
                    res["eval"] = {k: v[-1] for k, v in res["history"]["loss"].items()}
            return res

        results = self.get_ok_results(verbose=verbose)
        rp = [_flatten_dict(_delete_keys(add_eval(x), ignore_vals), separator) for x in results]
        df = pd.DataFrame.from_records(rp)

        first = ["tid", "loss", "status"]
        return _put_first(df, first)


# --------------------------------------------
def _train_and_eval_single(train, valid, model,
                           batch_size=32, epochs=300, use_weight=False,
                           callbacks=[], add_eval_metrics={}):
    """Fit and evaluate a keras model
    """
    def _format_keras_history(history):
        """nicely format keras history
        """
        return {"params": history.params,
                "loss": merge_dicts({"epoch": history.epoch}, history.history),
                }
    if use_weight:
        sample_weight = train[2]
    else:
        sample_weight = None
    # train the model
    logger.info("Fit...")
    history = History()
    model.fit(train[0], train[1],
              batch_size=batch_size,
              validation_data=valid[:2],
              epochs=epochs,
              sample_weight=sample_weight,
              verbose=2,
              callbacks=[history] + callbacks)

    return eval_model(model, valid, add_eval_metrics), _format_keras_history(history)


def eval_model(model, test, add_eval_metrics={}):
    # evaluate the model
    logger.info("Evaluate...")
    # - model_metrics
    model_metrics_values = model.evaluate(test[0], test[1], verbose=0)
    model_metrics = dict(zip(_listify(model.metrics_names),
                             _listify(model_metrics_values)))
    # - eval_metrics
    y_true = test[1]
    y_pred = model.predict(test[0], verbose=0)
    eval_metrics = {k: v(y_true, y_pred) for k, v in add_eval_metrics.items()}

    # handle the case where the two metrics names intersect
    # - omit duplicates from eval_metrics
    intersected_keys = set(model_metrics).intersection(set(eval_metrics))
    if len(intersected_keys) > 0:
        logger.warning("Some metric names intersect: {0}. Ignoring the add_eval_metrics ones".
                       format(intersected_keys))
        eval_metrics = _delete_keys(eval_metrics, intersected_keys)

    return merge_dicts(model_metrics, eval_metrics)


def get_model(model_fn, train_data, param):
    """Feed model_fn with train_data and param
    """
    model_param = merge_dicts({"train_data": train_data}, param["model"], param.get("shared", {}))
    return model_fn(**model_param)


def get_data(data_fn, param):
    """Feed data_fn with param
    """
    return data_fn(**merge_dicts(param["data"], param.get("shared", {})))


class CompileFN():
    # TODO - check if we can get (db_name, exp_name) from hyperopt

    def __init__(self, db_name, exp_name,
                 data_fn,
                 model_fn,
                 # validation metric
                 add_eval_metrics=[],
                 loss_metric="loss",  # val_loss
                 loss_metric_mode="min",
                 # validation split
                 valid_split=.2,
                 cv_n_folds=None,
                 stratified=False,
                 random_state=None,
                 # saving
                 use_tensorboard=True,
                 save_model=True,
                 save_results=True,
                 save_dir=DEFAULT_SAVE_DIR,
                 ):
        """
        # Arguments:
            add_eval_metrics: additional list of (global) evaluation
                metrics. Individual element can be
                a string (referring to concise.eval_metrics)
                or a function taking two numpy arrays: y_true, y_pred.
                These metrics are ment to supplement those specified in
                `model.compile(.., metrics = .)`.
            loss_metric: str, metric to monitor, must be in
                `add_eval_metrics` or `model.metrics_names`.
            loss_metric_mode: one of {min, max}. In `min` mode,
                training will stop when the metric
                monitored has stopped decreasing; in `max`
                mode it will stop when the metric
                monitored has stopped increasing; in `auto`
                mode, the direction is automatically inferred
                from the name of the monitored metric.
        """
        self.data_fn = data_fn
        self.model_fn = model_fn
        assert isinstance(add_eval_metrics, (list, tuple, set, dict))
        if isinstance(add_eval_metrics, dict):
            self.add_eval_metrics = {k: _get_ce_fun(v) for k, v in add_eval_metrics.items()}
        else:
            self.add_eval_metrics = {_to_string(fn_str): _get_ce_fun(fn_str)
                                     for fn_str in add_eval_metrics}
        assert isinstance(loss_metric, str)
        self.loss_metric = loss_metric
        assert loss_metric_mode in ["min", "max"]
        self.loss_metric_mode = loss_metric_mode

        # TODO - implement auto
        # if loss_metric_mode == "auto":
        #     # TODO - check where they are comming from
        #     if "acc" in loss_metric or \
        #        loss_metric.startswith("fmeasure") or \
        #        "var_explained" in loss_metric:
        #         metric_mode = "max"
        #     else:
        #         metric_mode = "min"

        self.data_name = data_fn.__code__.co_name
        self.model_name = model_fn.__code__.co_name
        self.db_name = db_name
        self.exp_name = exp_name
        # validation
        self.valid_split = valid_split
        self.cv_n_folds = cv_n_folds
        self.stratified = stratified
        self.random_state = random_state
        # saving
        self.use_tensorboard = use_tensorboard
        self.save_dir = save_dir
        self.save_model = save_model
        self.save_results = save_results

    @property
    def save_dir_exp(self):
        return self.save_dir + "/{db}/{exp}/".format(db=self.db_name, exp=self.exp_name)

    def _assert_loss_metric(self, model):
        model_metrics = _listify(model.metrics_names)
        eval_metrics = list(self.add_eval_metrics.keys())

        if self.loss_metric not in model_metrics + eval_metrics:
            raise ValueError("loss_metric: '{0}' not in ".format(self.loss_metric) +
                             "either sets of the losses: \n" +
                             "model.metrics_names: {0}\n".format(model_metrics) +
                             "add_eval_metrics: {0}".format(eval_metrics))

    def __call__(self, param):
        time_start = datetime.now()

        # set default early-stop parameters
        if param.get("fit") is None:
            param["fit"] = {}
        if param["fit"].get("epochs") is None:
            param["fit"]["epochs"] = 500
        # TODO - cleanup callback parameters
        #         - callbacks/early_stop/patience...
        if param["fit"].get("patience") is None:
            param["fit"]["patience"] = 10
        if param["fit"].get("batch_size") is None:
            param["fit"]["batch_size"] = 32
        if param["fit"].get("early_stop_monitor") is None:
            param["fit"]["early_stop_monitor"] = "val_loss"

        callbacks = [EarlyStopping(monitor=param["fit"]["early_stop_monitor"],
                                   patience=param["fit"]["patience"])]

        # setup paths for storing the data - TODO check if we can somehow get the id from hyperopt
        rid = str(uuid4())
        tm_dir = self.save_dir_exp + "/train_models/"
        os.makedirs(tm_dir, exist_ok=True)
        model_path = tm_dir + "{0}.h5".format(rid) if self.save_model else ""
        results_path = tm_dir + "{0}.json".format(rid) if self.save_results else ""

        if self.use_tensorboard:
            max_len = 240 - len(rid) - 1
            param_string = _dict_to_filestring(_flatten_dict_ignore(param))[:max_len] + ";" + rid
            tb_dir = self.save_dir_exp + "/tensorboard/" + param_string[:240]
            callbacks += [TensorBoard(log_dir=tb_dir,
                                      histogram_freq=0,  # TODO - set to some number afterwards
                                      write_graph=False,
                                      write_images=True)]
        # -----------------

        # get data
        logger.info("Load data...")
        train, _ = get_data(self.data_fn, param)
        time_data_loaded = datetime.now()

        # train & evaluate the model
        if self.cv_n_folds is None:
            # no cross-validation
            model = get_model(self.model_fn, train, param)
            print(_listify(model.metrics_names))
            self._assert_loss_metric(model)
            train_idx, valid_idx = split_train_test_idx(train,
                                                        self.valid_split,
                                                        self.stratified,
                                                        self.random_state)
            eval_metrics, history = _train_and_eval_single(train=subset(train, train_idx),
                                                           valid=subset(train, valid_idx),
                                                           model=model,
                                                           epochs=param["fit"]["epochs"],
                                                           batch_size=param["fit"]["batch_size"],
                                                           use_weight=param["fit"].get("use_weight", False),
                                                           callbacks=deepcopy(callbacks),
                                                           add_eval_metrics=self.add_eval_metrics)
            if model_path:
                model.save(model_path)
        else:
            # cross-validation
            eval_metrics_list = []
            history = []
            for i, (train_idx, valid_idx) in enumerate(split_KFold_idx(train,
                                                                       self.cv_n_folds,
                                                                       self.stratified,
                                                                       self.random_state)):
                logger.info("Fold {0}/{1}".format(i + 1, self.cv_n_folds))
                model = get_model(self.model_fn, subset(train, train_idx), param)
                self._assert_loss_metric(model)
                eval_m, history_elem = _train_and_eval_single(train=subset(train, train_idx),
                                                              valid=subset(train, valid_idx),
                                                              model=model,
                                                              epochs=param["fit"]["epochs"],
                                                              batch_size=param["fit"]["batch_size"],
                                                              use_weight=param["fit"].get("use_weight", False),
                                                              callbacks=deepcopy(callbacks),
                                                              add_eval_metrics=self.add_eval_metrics)
                print("\n")
                eval_metrics_list.append(eval_m)
                history.append(history_elem)
                if model_path:
                    model.save(model_path.replace(".h5", "_fold_{0}.h5".format(i)))
            # summarize metrics - take average accross folds
            eval_metrics = _mean_dict(eval_metrics_list)

        # get loss from eval_metrics
        loss = eval_metrics[self.loss_metric]
        if self.loss_metric_mode == "max":
            loss = - loss  # loss should get minimized

        time_end = datetime.now()

        ret = {"loss": loss,
               "status": STATUS_OK,
               "eval": eval_metrics,
               # additional info
               "param": param,
               "path": {
                   "model": model_path,
                   "results": results_path,
               },
               "name": {
                   "data": self.data_name,
                   "model": self.model_name,
                   "loss_metric": self.loss_metric,
                   "loss_metric_mode": self.loss_metric,
               },
               "history": history,
               # execution times
               "time": {
                   "start": str(time_start),
                   "end": str(time_end),
                   "duration": {
                       "total": (time_end - time_start).total_seconds(),  # in seconds
                       "dataload": (time_data_loaded - time_start).total_seconds(),
                       "training": (time_end - time_data_loaded).total_seconds(),
                   }}}

        # optionally save information to disk
        if results_path:
            write_json(ret, results_path)
        logger.info("Done!")
        return ret

    # Style guide:
    # -------------
    #
    # path structure:
    # /s/project/deepcis/hyperopt/db/exp/...
    #                                   /train_models/
    #                                   /best_model.h5

    # hyper-params format:
    #
    # data: ... (pre-preprocessing parameters)
    # model: (architecture, etc)
    # train: (epochs, patience...)


# --------------------------------------------
# helper functions


def _delete_keys(dct, keys):
    """Returns a copy of dct without `keys` keys
    """
    c = deepcopy(dct)
    assert isinstance(keys, list)
    for k in keys:
        c.pop(k)
    return c


def _mean_dict(dict_list):
    """Compute the mean value across a list of dictionaries
    """
    return {k: np.array([d[k] for d in dict_list]).mean()
            for k in dict_list[0].keys()}


def _put_first(df, names):
    df = df.reindex(columns=names + [c for c in df.columns if c not in names])
    return df


def _listify(arg):
    if hasattr(type(arg), '__len__'):
        return arg
    return [arg, ]


def _to_string(fn_str):
    if isinstance(fn_str, str):
        return fn_str
    elif callable(fn_str):
        return fn_str.__name__
    else:
        raise ValueError("fn_str has to be callable or str")


def _get_ce_fun(fn_str):
    if isinstance(fn_str, str):
        return ce.get(fn_str)
    elif callable(fn_str):
        return fn_str
    else:
        raise ValueError("fn_str has to be callable or str")

def _flatten_dict(dd, separator='_', prefix=''):
    return {prefix + separator + k if prefix else k: v
            for kk, vv in dd.items()
            for k, v in _flatten_dict(vv, separator, kk).items()
            } if isinstance(dd, dict) else {prefix: dd}

def _flatten_dict_ignore(dd, prefix=''):
    return {k if prefix else k: v
            for kk, vv in dd.items()
            for k, v in _flatten_dict_ignore(vv, kk).items()
            } if isinstance(dd, dict) else {prefix: dd}

def _dict_to_filestring(d):
    def to_str(v):
        if isinstance(v, float):
            return '%s' % float('%.2g' % v)
        else:
            return str(v)

    return ";".join([k + "=" + to_str(v) for k, v in d.items()])
