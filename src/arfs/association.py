""" Parallelized Association and Correlation matrix
 
This module provides parallelized methods for computing associations.
Namely, correlation, correlation ratio, Theil's U, Cramer's V

They are the basis of the MRmr feature selection
"""

import math
import warnings
import matplotlib
import numpy as np
import gc
import pandas as pd
import scipy.stats as ss
import matplotlib.pyplot as plt
import scipy.cluster.hierarchy as sch
import scipy.stats as ss


from mpl_toolkits.axes_grid1 import make_axes_locatable
from sklearn.utils import as_float_array, safe_sqr, safe_mask
from multiprocessing import cpu_count
from itertools import combinations, permutations, product
from pandas.api.types import is_numeric_dtype
from scipy.stats import rankdata
from functools import partial

from .parallel import (
    parallel_matrix_entries,
    parallel_df,
    _compute_series,
    _compute_matrix_entries,
)
from .utils import create_dtype_dict

_PRECISION = 1e-13

########################
# Redundancy measures
########################

# For computing the redundancy of all the columns with a given series
# R(y, x_i) for i=1,..., N --> a series (y is a fixed, chosen column)
# the main functions are:
# - the series-series computation (two columns)
# - the closure for applying the latter function to all columns of a dataframe
# - the "series" version, using the closure for computing the redundancy with all the cols of the DF
#
# For computing the redundancy matrix all the cols combinations of columns
# R(x_i, x_j) for i, j=1,..., N --> a data frame (either TRIUL if the measure is symmetric
# or the full matrix if asymmetric)
# - the series-series computation (two columns), same as for series case
# - the function looping over a chunk of combinations
# - the parallelization (sending different chunks to different cores and applying the latter function)

##################
# CAT-CAT
##################


def weighted_conditional_entropy(x, y, sample_weight=None):
    """weighted_conditional_entropy computes the weighted conditional entropy between two
    categorical predictors.

    Parameters
    ----------
    x : pd.Series of shape (n_samples,)
        The predictor vector
    y : pd.Series of shape (n_samples,)
        The target vector
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None

    Returns
    -------
    float
        weighted conditional entropy
    """

    if sample_weight is None:
        sample_weight = np.ones(len(x))
    elif np.count_nonzero(sample_weight) == 0:
        raise ValueError(
            "All elements in sample_weight are zero. Cannot divide by zero."
        )

    df = pd.DataFrame({"x": x, "y": y, "sample_weight": sample_weight})
    tot_weight = df["sample_weight"].sum()
    y_counter = df[["y", "sample_weight"]].groupby("y").sum().to_dict()
    y_counter = y_counter["sample_weight"]
    xy_counter = df[["x", "y", "sample_weight"]].groupby(["x", "y"]).sum().to_dict()
    xy_counter = xy_counter["sample_weight"]
    h_xy = 0.0
    for xy in xy_counter.keys():
        p_xy = xy_counter[xy] / tot_weight if tot_weight != 0 else 0
        p_y = y_counter[xy[1]] / tot_weight if tot_weight != 0 else 0
        if p_xy != 0:
            h_xy += p_xy * math.log(p_y / p_xy, math.e)
    return h_xy


def weighted_theils_u(x, y, sample_weight=None, as_frame=False):
    """weighted_theils_u computes the weighted Theil's U statistic between two
    categorical predictors.

    Parameters
    ----------
    x : pd.Series of shape (n_samples,)
        The predictor vector
    y : pd.Series of shape (n_samples,)
        The target vector
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    as_frame: bool
        return output as a dataframe or a float

    Returns
    -------
    pd.DataFrame
        predictor names and value of the Theil's U statistic
    """

    if sample_weight is None:
        sample_weight = np.ones(len(x))

    df = pd.DataFrame({"x": x, "y": y, "sample_weight": sample_weight})
    tot_weight = df["sample_weight"].sum()
    y_counter = df[["y", "sample_weight"]].groupby("y").sum().to_dict()
    y_counter = y_counter["sample_weight"]
    x_counter = df[["x", "sample_weight"]].groupby("x").sum().to_dict()
    x_counter = x_counter["sample_weight"]
    p_x = list(map(lambda n: n / tot_weight, x_counter.values()))
    h_x = ss.entropy(p_x)
    xy_counter = df[["x", "y", "sample_weight"]].groupby(["x", "y"]).sum().to_dict()
    xy_counter = xy_counter["sample_weight"]
    h_xy = 0.0
    for xy in xy_counter.keys():
        p_xy = xy_counter[xy] / tot_weight if tot_weight != 0 else 0
        p_y = y_counter[xy[1]] / tot_weight if tot_weight != 0 else 0
        if p_xy != 0:
            h_xy += p_xy * math.log(p_y / p_xy, math.e)

    if h_x == 0:
        return 1.0
    else:
        u = (h_x - h_xy) / h_x
        if abs(u) < _PRECISION or abs(u - 1.0) < _PRECISION:
            rounded_u = round(u)
            warnings.warn(
                f"Rounded U = {u} to {rounded_u}. This is probably due to floating point precision issues.",
                RuntimeWarning,
            )
            u = rounded_u

    if as_frame:
        return pd.DataFrame({"row": x.name, "col": y.name, "val": u}, index=[0])
    else:
        return u


def theils_u_matrix(X, sample_weight=None, n_jobs=-1, handle_na="drop"):
    """theils_u_matrix theils_u_matrix computes the weighted Theil's U statistic for
    categorical-categorical association. This is an asymmetric coefficient: U(x,y) != U(y,x)
    U(x, y) means the uncertainty of x given y: value is on the range of [0,1] -
    where 0 means y provides no information about x, and 1 means y provides full information about x

    The computation is embarrassingly parallel and is distributed on available cores.
    Moreover, the statistic is computed for the unique combinations only and returned in a
    tidy (long) format.

    Parameters
    ----------
    X :  array-like of shape (n_samples, n_features)
        predictor dataframe
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    n_jobs : int, optional
        the number of cores to use for the computation, by default -1
    handle_na : str, optional
        either drop rows with na, fill na with 0 or do nothing, by default "drop"

    Returns
    -------
    pd.DataFrame
        The Theil's U matrix in a tidy (long) format.
    """

    # sanity checks
    X, sample_weight = _check_association_input(X, sample_weight, handle_na)

    # Cramer's V only for categorical columns
    col_dtypes_dic = create_dtype_dict(X)
    dtypes_dic = create_dtype_dict(X, dic_keys="dtypes")
    cat_cols = dtypes_dic["cat"]

    if cat_cols and (len(cat_cols) >= 2):
        # explicitely store the unique 2-permutation of column names
        # permutations and not combinations because U is asymmetric
        comb_list = [comb for comb in permutations(cat_cols, 2)]
        # define the number of cores
        n_jobs = (
            min(cpu_count(), len(cat_cols))
            if n_jobs == -1
            else min(cpu_count(), n_jobs)
        )
        # parallelize jobs
        theil_u_matrix_entries = partial(
            _compute_matrix_entries, func_xyw=weighted_theils_u
        )
        lst = parallel_matrix_entries(
            func=theil_u_matrix_entries,
            df=X,
            comb_list=comb_list,
            sample_weight=sample_weight,
            n_jobs=-1,
        )
        return lst
    else:
        return None


def theils_u_series(X, target, sample_weight=None, n_jobs=-1, handle_na="drop"):
    """theils_u_series computes the weighted Theil's U statistic for
    categorical-categorical association. This is an asymmetric coefficient: U(x,y) != U(y,x)
    U(x, y) means the uncertainty of x given y: value is on the range of [0,1] -
    where 0 means y provides no information about x, and 1 means y provides full information about x

    The computation is embarrassingly parallel and is distributed on available cores.
    Moreover, the statistic is computed for the unique combinations only and returned in a
    tidy (long) format.

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)
        predictor dataframe
    target : str or int
        the predictor name or index with which to compute association
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    n_jobs : int, optional
        the number of cores to use for the computation, by default -1
    handle_na : str, optional
        either drop rows with na, fill na with 0 or do nothing, by default "drop"

    Returns
    -------
    pd.Series
        The Theil's U series.
    """
    # sanity checks
    X, sample_weight = _check_association_input(X, sample_weight, handle_na)
    col_dtypes_dic = create_dtype_dict(X)
    dtypes_dic = create_dtype_dict(X, dic_keys="dtypes")

    if col_dtypes_dic[target] != "cat":
        raise TypeError("the target column is not categorical")

    # Cramer's V only for categorical columns
    cat_cols = dtypes_dic["cat"]

    if cat_cols:
        # define the number of cores
        n_jobs = (
            min(cpu_count(), len(cat_cols))
            if n_jobs == -1
            else min(cpu_count(), n_jobs)
        )
        # parallelize jobs
        _theil_u = partial(_compute_series, func_xyw=weighted_theils_u)
        lst = parallel_df(
            func=_theil_u,
            df=X[cat_cols],
            series=X[target],
            sample_weight=sample_weight,
            n_jobs=n_jobs,
        )
        return lst
    else:
        return None


def cramer_v(x, y, sample_weight=None, as_frame=False):
    """cramer_v computes the weighted V statistic of two
    categorical predictors.

    Parameters
    ----------
    x : pd.Series of shape (n_samples,)
        The predictor vector, the first categorical predictor
    y : pd.Series of shape (n_samples,)
        second categorical predictor, order doesn't matter, symmetrical association
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    as_frame: bool
        return output as a dataframe or a float

    Returns
    -------
    pd.DataFrame
        single row dataframe with the predictor names and the statistic value
    """
    tot_weight = sample_weight.sum()
    weighted_tab = pd.crosstab(x, y, sample_weight, aggfunc=sum).fillna(0)
    chi2 = ss.chi2_contingency(weighted_tab)[0]
    phi2 = chi2 / tot_weight
    r, k = weighted_tab.shape
    phi2corr = max(0, phi2 - ((k - 1) * (r - 1)) / (tot_weight - 1))
    rcorr = r - ((r - 1) ** 2) / (tot_weight - 1)
    kcorr = k - ((k - 1) ** 2) / (tot_weight - 1)
    v = np.sqrt(phi2corr / min((kcorr - 1), (rcorr - 1)))

    if as_frame:
        x_name = x.name if isinstance(x, pd.Series) else "var"
        y_name = y.name if isinstance(y, pd.Series) else "target"
        return pd.DataFrame(
            {"row": [x_name, y_name], "col": [y_name, x_name], "val": [v, v]}
        )
    else:
        return v


def cramer_v_matrix(X, sample_weight=None, n_jobs=-1, handle_na="drop"):
    """cramer_v_matrix computes the weighted Cramer's V statistic for
    categorical-categorical association. This is a symmetric coefficient: V(x,y) = V(y,x)

    It uses the corrected Cramer's V statistics, itself based on the chi2 contingency
    table. The computation is embarrassingly parallel and is distributed on available cores.
    Moreover, the statistic is computed for the unique combinations only and returned in a
    tidy (long) format.

    Parameters
    ----------
    X :  array-like of shape (n_samples, n_features)
        predictor dataframe
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    n_jobs : int, optional
        the number of cores to use for the computation, by default -1
    handle_na : str, optional
        either drop rows with na, fill na with 0 or do nothing, by default "drop"

    Returns
    -------
    pd.DataFrame
        The Cramer's V matrix (lower triangular) in a tidy (long) format.
    """
    # sanity checks
    X, sample_weight = _check_association_input(X, sample_weight, handle_na)
    dtypes_dic = create_dtype_dict(X, dic_keys="dtypes")

    # Cramer's V only for categorical columns
    # in GLM supposed to be all the columns
    cat_cols = dtypes_dic["cat"]

    if cat_cols and (len(cat_cols) >= 2):
        # explicitely store the unique 2-combinations of column names
        comb_list = [comb for comb in combinations(cat_cols, 2)]
        # define the number of cores
        n_jobs = (
            min(cpu_count(), len(cat_cols))
            if n_jobs == -1
            else min(cpu_count(), n_jobs)
        )
        _cramer_v_matrix_entries = partial(_compute_matrix_entries, func_xyw=cramer_v)
        lst = parallel_matrix_entries(
            func=_cramer_v_matrix_entries,
            df=X,
            comb_list=comb_list,
            sample_weight=sample_weight,
            n_jobs=n_jobs,
        )
        return lst
    else:
        return None


def cramer_v_series(X, target, sample_weight=None, n_jobs=-1, handle_na="drop"):
    """cramer_v_series computes the weighted Cramer's V statistic for
    categorical-categorical association. This is a symmetric coefficient: V(x,y) = V(y,x)

    It uses the corrected Cramer's V statistics, itself based on the chi2 contingency
    table. The computation is embarrassingly parallel and is distributed on available cores.
    Moreover, the statistic is computed for the unique combinations only and returned in a
    tidy (long) format.

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)
        predictor dataframe
    target : str or int
        the predictor name or index with which to compute association
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    n_jobs : int, optional
        the number of cores to use for the computation, by default -1
    handle_na : str, optional
        either drop rows with na, fill na with 0 or do nothing, by default "drop"

    Returns
    -------
    pd.Series
        The Cramer's V series
    """
    # sanity checks
    X, sample_weight = _check_association_input(X, sample_weight, handle_na)
    col_dtypes_dic = create_dtype_dict(X)
    dtypes_dic = create_dtype_dict(X, dic_keys="dtypes")

    if col_dtypes_dic[target] != "cat":
        raise TypeError("the target column is not categorical")

    # Cramer's V only for categorical columns
    cat_cols = dtypes_dic["cat"]

    if cat_cols:
        X = X[cat_cols]
        # define the number of cores
        n_jobs = (
            min(cpu_count(), len(cat_cols))
            if n_jobs == -1
            else min(cpu_count(), n_jobs)
        )
        # parallelize jobs
        _cramer_v = partial(_compute_series, func_xyw=cramer_v)
        lst = parallel_df(
            func=_cramer_v,
            df=X[cat_cols],
            series=X[target],
            sample_weight=sample_weight,
            n_jobs=n_jobs,
        )
        # concatenate the results
        # v_df_list = list(chain(*v_df_list))
        return lst  # pd.concat(lst)
    else:
        return None


def _weighted_correlation_ratio(*args):
    """Calculates the Correlation Ratio (sometimes marked by the greek letter Eta)
    for categorical-continuous association.
    Answers the question - given a continuous value of a measurement, is it
    possible to know which category is it associated with?
    Value is in the range [0,1], where 0 means a category cannot be determined
    by a continuous measurement, and 1 means a category can be determined with
    absolute certainty.

    Based on the scikit-learn implementation of the unweighted version.

    Returns
    -------
    float
        value of the correlation ratio
    """
    # how many levels (predictor)
    n_classes = len(args)
    # convert to float 2-uple d'array
    args = [as_float_array(a) for a in args]
    # compute the total weight per level
    weight_per_class = np.array([a[1].sum() for a in args])
    # total weight
    tot_weight = np.sum(weight_per_class)
    # weighted sum of squares
    ss_alldata = sum((a[1] * safe_sqr(a[0])).sum(axis=0) for a in args)
    # list of weighted sums
    sums_args = [np.asarray((a[0] * a[1]).sum(axis=0)) for a in args]
    square_of_sums_alldata = sum(sums_args) ** 2
    square_of_sums_args = [s**2 for s in sums_args]
    sstot = ss_alldata - square_of_sums_alldata / float(tot_weight)
    ssbn = 0.0
    for k, _ in enumerate(args):
        ssbn += square_of_sums_args[k] / weight_per_class[k]
    ssbn -= square_of_sums_alldata / float(tot_weight)
    constant_features_idx = np.where(sstot == 0.0)[0]
    if np.nonzero(ssbn)[0].size != ssbn.size and constant_features_idx.size:
        warnings.warn("Features %s are constant." % constant_features_idx, UserWarning)
    etasq = ssbn / sstot
    # flatten matrix to vector in sparse case
    etasq = np.asarray(etasq).ravel()
    return np.sqrt(etasq)


def correlation_ratio(x, y, sample_weight=None, as_frame=False):
    """Compute the weighted correlation ratio. The association between a continuous predictor (y)
    and a categorical predictor (x). It can be weighted.

    Parameters
    ----------
    x : pd.Series of shape (n_samples,)
        The categorical predictor vector
    y : pd.Series of shape (n_samples,)
        The continuous predictor
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    as_frame: bool
        return output as a dataframe or a float

    Returns
    -------
    float
        value of the correlation ratio
    """
    if sample_weight is None:
        sample_weight = np.ones_like(y)

    # one 2-uple per level of the categorical feature x
    if x.dtype in ["category", "object", "bool"]:
        args = [
            (
                y[safe_mask(y, x == k)],
                sample_weight[safe_mask(sample_weight, x == k)],
            )
            for k in np.unique(x)
        ]
    elif y.dtype in ["category", "object", "bool"]:
        args = [
            (
                x[safe_mask(x, y == k)],
                sample_weight[safe_mask(sample_weight, y == k)],
            )
            for k in np.unique(y)
        ]
    else:
        raise TypeError(
            "one of the two series should be categorical/object and the other numerical"
        )

    if as_frame:
        x_name = x.name if isinstance(x, pd.Series) else "var"
        y_name = y.name if isinstance(y, pd.Series) else "target"
        v = _weighted_correlation_ratio(*args)[0]
        return pd.DataFrame(
            {"row": [x_name, y_name], "col": [y_name, x_name], "val": [v, v]}
        )
    else:
        return _weighted_correlation_ratio(*args)[0]


def correlation_ratio_matrix(X, sample_weight=None, n_jobs=-1, handle_na="drop"):
    """correlation_ratio_matrix computes the weighted Correlation Ratio for
    categorical-numerical association. This is a symmetric coefficient: CR(x,y) = CR(y,x)

    The computation is embarrassingly parallel and is distributed on available cores.
    Moreover, the statistic is computed for the unique combinations only and returned in a
    tidy (long) format.

    Parameters
    ----------
    X :  array-like of shape (n_samples, n_features)
        predictor dataframe
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    n_jobs : int, optional
        the number of cores to use for the computation, by default -1
    handle_na : str, optional
        either drop rows with na, fill na with 0 or do nothing, by default "drop"

    Returns
    -------
    pd.DataFrame
        The correlation ratio matrix (lower triangular) in a tidy (long) format.
    """
    # sanity checks
    X, sample_weight = _check_association_input(X, sample_weight, handle_na)

    # Cramer's V only for categorical columns
    dtypes_dic = create_dtype_dict(X, dic_keys="dtypes")
    cat_cols = dtypes_dic["cat"]
    num_cols = dtypes_dic["num"]

    if cat_cols and num_cols:
        # explicitely store the unique 2-combinations of column names
        # the first one should be the categorical predictor
        comb_list = list(product(cat_cols, num_cols))
        # define the number of cores
        n_jobs = (
            min(cpu_count(), len(cat_cols))
            if n_jobs == -1
            else min(cpu_count(), n_jobs)
        )
        # parallelize jobs
        corr_ratio_matrix_entries = partial(
            _compute_matrix_entries, func_xyw=correlation_ratio
        )
        lst = parallel_matrix_entries(
            func=corr_ratio_matrix_entries,
            df=X,
            comb_list=comb_list,
            sample_weight=sample_weight,
            n_jobs=-1,
        )
        return lst
    else:
        return None


def correlation_ratio_series(
    X, target, sample_weight=None, n_jobs=-1, handle_na="drop"
):
    """correlation_ratio_series computes the weighted correlation ration for
    categorical-numerical association. This is a symmetric coefficient: CR(x,y) = CR(y,x)

    The computation is embarrassingly parallel and is distributed on available cores.
    Moreover, the statistic is computed for the unique combinations only and returned in a
    tidy (long) format, a series.

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)
        predictor dataframe
    target : str or int
        the predictor name or index with which to compute association
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    n_jobs : int, optional
        the number of cores to use for the computation, by default -1
    handle_na : str, optional
        either drop rows with na, fill na with 0 or do nothing, by default "drop"

    Returns
    -------
    pd.Series
        The Correlation ratio series (lower triangular) in a tidy (long) format.
    """
    # sanity checks
    X, sample_weight = _check_association_input(X, sample_weight, handle_na)
    col_dtypes_dic = create_dtype_dict(X)
    dtypes_dic = create_dtype_dict(X, dic_keys="dtypes")

    if col_dtypes_dic[target] == "cat":
        # if the target is categorical, pick only num predictors
        pred_list = dtypes_dic["num"]
    else:
        # if the target is numerical, the 2nd pred should be categorical
        pred_list = dtypes_dic["cat"]

    if pred_list:
        # define the number of cores
        n_jobs = (
            min(cpu_count(), len(pred_list))
            if n_jobs == -1
            else min(cpu_count(), n_jobs)
        )
        # parallelize jobs
        _corr_ratio = partial(_compute_series, func_xyw=correlation_ratio)
        lst = parallel_df(
            func=_corr_ratio,
            df=X[pred_list],
            series=X[target],
            sample_weight=sample_weight,
            n_jobs=n_jobs,
        )
        return lst
    else:
        return None


def wm(x, w):
    """wm computes the weighted mean

    Parameters
    ----------
    x : array-like of shape (n_samples,)
        the target array
    w : array-like of shape (n_samples,)
        the sample weights array

    Returns
    -------
    float
        weighted mean
    """
    return np.sum(x * w) / np.sum(w)


def wcov(x, y, w):
    """wcov computes the weighted covariance

    Parameters
    ----------
    x : array-like of shape (n_samples,)
        the perdictor 1 array
    y : array-like of shape (n_samples,)
        the perdictor 2 array
    w : array-like of shape (n_samples,)
        the sample weights array

    Returns
    -------
    float
        weighted covariance
    """
    return np.sum(w * (x - wm(x, w)) * (y - wm(y, w))) / np.sum(w)


def wcorr(x, y, w):
    """wcov computes the weighted Pearson correlation coefficient

    Parameters
    ----------
    x : array-like of shape (n_samples,)
        the perdictor 1 array
    y : array-like of shape (n_samples,)
        the perdictor 2 array
    w : array-like of shape (n_samples,)
        the sample weights array

    Returns
    -------
    float
        weighted correlation coefficient
    """
    return wcov(x, y, w) / np.sqrt(wcov(x, x, w) * wcov(y, y, w))


def wrank(x, w):
    """wrank computes the weighted rank

    Parameters
    ----------
    x : array-like of shape (n_samples,)
        the target array
    w : array-like of shape (n_samples,)
        the sample weights array

    Returns
    -------
    float
        weighted rank
    """
    (unique, arr_inv, counts) = np.unique(
        rankdata(x), return_counts=True, return_inverse=True
    )
    a = np.bincount(arr_inv, w)
    return (np.cumsum(a) - a)[arr_inv] + ((counts + 1) / 2 * (a / counts))[arr_inv]


def wspearman(x, y, w):
    """wcov computes the weighted Spearman correlation coefficient

    Parameters
    ----------
    x : array-like of shape (n_samples,)
        the perdictor 1 array
    y : array-like of shape (n_samples,)
        the perdictor 2 array
    w : array-like of shape (n_samples,)
        the sample weights array

    Returns
    -------
    float
        Spearman weighted correlation coefficient
    """
    return wcorr(wrank(x, w), wrank(y, w), w)


def weighted_corr(x, y, sample_weight=None, as_frame=False, method="pearson"):
    """weighted_corr computes the weighted correlation coefficient (Pearson or Spearman)

    Parameters
    ----------
    x : pd.Series of shape (n_samples,)
        The categorical predictor vector
    y : pd.Series of shape (n_samples,)
        The continuous predictor
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    as_frame: bool
        return output as a dataframe or a float
    method : str
        either "spearman" or "pearson", by default "pearson"

    Returns
    -------
    float or pd.DataFrame
        weighted correlation coefficient
    """

    if method == "pearson":
        c = wcorr(x, y, sample_weight)
    else:
        c = wspearman(x, y, sample_weight)

    if as_frame:
        x_name = x.name if isinstance(x, pd.Series) else "var"
        y_name = y.name if isinstance(y, pd.Series) else "target"
        return pd.DataFrame(
            {"row": [x_name, y_name], "col": [y_name, x_name], "val": [c, c]}
        )
    else:
        return c


def wcorr_series(
    X, target, sample_weight=None, n_jobs=-1, handle_na="drop", method="pearson"
):
    """wcorr_series computes the weighted correlation coefficient (Pearson or Spearman) for
    continuous-continuous association. This is an symmetric coefficient: corr(x,y) = corr(y,x)

    The computation is embarrassingly parallel and is distributed on available cores.
    Moreover, the statistic is computed for the unique combinations only and returned in a
    tidy (long) format.

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)
        predictor dataframe
    target : str or int
        the predictor name or index with which to compute association
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    n_jobs : int, optional
        the number of cores to use for the computation, by default -1
    handle_na : str, optional
        either drop rows with na, fill na with 0 or do nothing, by default "drop"
    method : str
        either "spearman" or "pearson", by default "pearson"

    Returns
    -------
    pd.Series
        The weighted correlation series.
    """
    # sanity checks
    X, sample_weight = _check_association_input(X, sample_weight, handle_na)
    col_dtypes_dic = create_dtype_dict(X)
    dtypes_dic = create_dtype_dict(X, dic_keys="dtypes")

    if col_dtypes_dic[target] == "cat":
        raise TypeError("the target column is categorical")

    num_cols = dtypes_dic["num"]
    y = X[target]

    if num_cols:
        _wcorr_method = partial(weighted_corr, method=method)
        # parallelize jobs
        _wcorr_method_series = partial(_compute_series, func_xyw=_wcorr_method)
        return parallel_df(
            func=_wcorr_method_series,
            df=X[num_cols],
            series=y,
            sample_weight=sample_weight,
            n_jobs=n_jobs,
        )
    else:
        return None


def wcorr_matrix(X, sample_weight=None, n_jobs=-1, handle_na="drop", method="pearson"):
    """wcorr_matrix computes the weighted correlation statistic for
    (Pearson or Spearman) for continuous-continuous association.
    This is an symmetric coefficient: corr(x,y) = corr(y,x)

    The computation is embarrassingly parallel and is distributed on available cores.
    Moreover, the statistic is computed for the unique combinations only and returned in a
    tidy (long) format.

    Parameters
    ----------
    X :  array-like of shape (n_samples, n_features)
        predictor dataframe
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    n_jobs : int, optional
        the number of cores to use for the computation, by default -1
    handle_na : str, optional
        either drop rows with na, fill na with 0 or do nothing, by default "drop"
    method : str
        either "spearman" or "pearson"

    Returns
    -------
    pd.DataFrame
        The Cramer's V matrix (lower triangular) in a tidy (long) format.
    """
    # sanity checks
    X, sample_weight = _check_association_input(X, sample_weight, handle_na)
    col_dtypes_dic = create_dtype_dict(X)
    dtypes_dic = create_dtype_dict(X, dic_keys="dtypes")

    num_cols = dtypes_dic["num"]
    if num_cols:
        # explicitely store the unique 2-combinations of column names
        comb_list = [comb for comb in combinations(num_cols, 2)]
        # define the number of cores
        n_jobs = (
            min(cpu_count(), len(num_cols))
            if n_jobs == -1
            else min(cpu_count(), n_jobs)
        )

        if (n_jobs > 1) or (method != "pearson"):
            _wcorr_method = partial(weighted_corr, method=method)
            _wcorr_method_entries = partial(
                _compute_matrix_entries, func_xyw=_wcorr_method
            )
            lst = parallel_matrix_entries(
                func=_wcorr_method_entries,
                df=X,
                comb_list=comb_list,
                sample_weight=sample_weight,
                n_jobs=-1,
            )
            return lst
        else:
            return (
                matrix_to_xy(weighted_correlation_1cpu(X, sample_weight, handle_na))
                .to_frame()
                .reset_index()
                .rename(columns={"level_0": "row", "level_1": "col", 0: "val"})
            )
    else:
        return None


def weighted_correlation_1cpu(X, sample_weight=None, handle_na="drop"):
    """weighted_correlation computes the lower triangular weighted correlation matrix
    using a single CPU, therefore using common numpy linear algebra

    Parameters
    ----------
    X :  array-like of shape (n_samples, n_features)
        predictor dataframe
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    handle_na : str, optional
        either drop rows with na, fill na with 0 or do nothing, by default "drop"

    Returns
    -------
    pd.DataFrame
        the lower triangular weighted correlation matrix in long format
    """
    # sanity checks
    X, sample_weight = _check_association_input(X, sample_weight, handle_na)
    # degree of freedom for the second moment estimator
    ddof = 1

    col_dtypes_dic = create_dtype_dict(X)
    dtypes_dic = create_dtype_dict(X, dic_keys="dtypes")

    numeric_cols = dtypes_dic["num"]

    if numeric_cols:
        data = X[numeric_cols]
        col_idx = data.columns
        data = data.values
        sum_weights = sample_weight.sum()
        weighted_sum = np.dot(data.T, sample_weight)
        weighted_mean = weighted_sum / sum_weights
        demeaned = data - weighted_mean
        sum_of_squares = np.dot((demeaned**2).T, sample_weight)
        weighted_std = np.sqrt(sum_of_squares / (sum_weights - ddof))
        weighted_cov = np.dot(sample_weight * demeaned.T, demeaned)
        weighted_cov /= sum_weights - ddof
        weighted_corcoef = pd.DataFrame(
            weighted_cov / weighted_std / weighted_std[:, None],
            index=col_idx,
            columns=col_idx,
        )
        return weighted_corcoef
    else:
        return None


#################################
# association
# cat-cat + cat-cont + cont-cont
#################################


def association_series(
    X,
    target,
    features=None,
    sample_weight=None,
    nom_nom_assoc="theil",
    num_num_assoc="pearson",
    nom_num_assoc="correlation_ratio",
    normalize=False,
    n_jobs=-1,
    handle_na="drop",
):
    """association_series computes the association matrix for cont-cont, cat-cont and cat-cat.
    predictors. The weighted correlation matrix is used for the cont-cont predictors.
    The correlation ratio is used between cont-cat predictors and either the Cramer's V or Theil's U
    matrix for cat-cat predictors. The Pearson or Spearman correlation coefficient is used for
    the cont-cont association.

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)
        predictor dataframe
    target : str or int
        the predictor name or index with which to compute association
    features : list of str, optional
        list of features with which to compute the association
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    nom_nom_assoc : str or callable
        If callable, a function which receives two `pd.Series` (and optionally a weight array) and returns a single number.
        If string, name of nominal-nominal (categorical-categorical) association to use.
        Options are 'cramer' for Cramer's V or `theil` for Theil's U. If 'theil',
        heat-map columns are the provided information (U = U(row|col)).
    num_num_assoc : str or callable
        If callable, a function which receives two `pd.Series` and returns a single number.
        If string, name of numerical-numerical association to use. Options are 'pearson'
        for Pearson's R, 'spearman' for Spearman's R.
    nom_num_assoc : str or callable
        If callable, a function which receives two `pd.Series` and returns a single number.
        If string, name of nominal-numerical association to use. Options are 'correlation_ratio'
        for correlation ratio
    normalize : bool
        either to normalize or not the scores
    n_jobs : int, optional
        the number of cores to use for the computation, by default -1
    handle_na : str, optional
        either drop rows with na, fill na with 0 or do nothing, by default "drop"

    Returns
    -------
    pd.Series
        a series with all the association values with the target column

    Raises
    ------
    TypeError
        if features is not None and is not a list of strings
    """
    # sanity checks
    X, sample_weight = _check_association_input(X, sample_weight, handle_na)

    if features and is_list_of_str(features):
        data = X[features + [target]]
    elif features and (not is_list_of_str(features)):
        raise TypeError("features is not a list of strings")
    elif features is None:
        data = X

    dtypes_dic = create_dtype_dict(X)

    # only numeric, NaN already checked, not repeating the process
    if X.dtypes.map(is_numeric_dtype).all():
        if callable(num_num_assoc):
            return _callable_association_series_fn(
                assoc_fn=num_num_assoc,
                X=data,
                target=target,
                sample_weight=sample_weight,
                n_jobs=n_jobs,
                kind="num-num",
            )
        else:
            return wcorr_series(
                data,
                target,
                sample_weight,
                n_jobs,
                handle_na=None,
                method=num_num_assoc,
            )

    # only categorical (here understood as no numerical columns)
    if not X.dtypes.map(is_numeric_dtype).any():
        if callable(nom_nom_assoc):
            return _callable_association_series_fn(
                assoc_fn=nom_nom_assoc,
                X=data,
                target=target,
                sample_weight=sample_weight,
                n_jobs=n_jobs,
                kind="nom-nom",
            )
        elif nom_nom_assoc == "theil":
            return theils_u_series(data, target, sample_weight, n_jobs, handle_na=None)
        elif nom_nom_assoc == "cramer":
            return cramer_v_series(data, target, sample_weight, n_jobs, handle_na=None)

    # cat-num
    if callable(nom_num_assoc):
        assoc_series = _callable_association_series_fn(
            assoc_fn=nom_num_assoc,
            X=data,
            target=target,
            sample_weight=sample_weight,
            n_jobs=n_jobs,
            kind="nom-num",
        )
    else:
        assoc_series = correlation_ratio_series(
            data, target, sample_weight, n_jobs, handle_na=None
        )

    if normalize:
        assoc_series = (assoc_series - assoc_series.min()) / np.ptp(assoc_series)

    # cat-cat
    if dtypes_dic[target] == "cat":
        if callable(nom_nom_assoc):
            assoc_series_complement = _callable_association_series_fn(
                assoc_fn=nom_nom_assoc,
                X=data,
                target=target,
                sample_weight=sample_weight,
                n_jobs=n_jobs,
                kind="nom-nom",
            )
        elif nom_nom_assoc == "theil":
            assoc_series_complement = theils_u_series(
                data, target, sample_weight, n_jobs, handle_na=None
            )
        else:
            assoc_series_complement = cramer_v_series(
                data, target, sample_weight, n_jobs, handle_na=None
            )

        if normalize:
            assoc_series_complement = (
                assoc_series_complement - assoc_series_complement.min()
            ) / np.ptp(assoc_series_complement)

        assoc_series = pd.concat([assoc_series, assoc_series_complement])

    # num-num
    if dtypes_dic[target] == "num":
        if callable(num_num_assoc):
            assoc_series_complement = _callable_association_series_fn(
                assoc_fn=num_num_assoc,
                X=data,
                target=target,
                sample_weight=sample_weight,
                n_jobs=n_jobs,
                kind="num-num",
            )
        else:
            assoc_series_complement = wcorr_series(
                data,
                target,
                sample_weight,
                n_jobs,
                handle_na=None,
                method=num_num_assoc,
            )

        if normalize:
            assoc_series_complement = (
                assoc_series_complement - assoc_series_complement.min()
            ) / np.ptp(assoc_series_complement)

        assoc_series = pd.concat([assoc_series, assoc_series_complement])

    return assoc_series.sort_values(ascending=False)


def association_matrix(
    X,
    sample_weight=None,
    nom_nom_assoc="theil",
    num_num_assoc="pearson",
    nom_num_assoc="correlation_ratio",
    n_jobs=-1,
    handle_na="drop",
    nom_nom_comb=None,
    num_num_comb=None,
    nom_num_comb=None,
):
    """association_matrix computes the association matrix for cont-cont, cat-cont and cat-cat.
    predictors. The weighted correlation matrix is used for the cont-cont predictors.
    The correlation ratio is used between cont-cat predictors and either the Cramer's V or Theil's U
    matrix for cat-cat predictors.

    The association matrix is not symmetric is Theil is used. The obeservations might be weighted.

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)
        predictor dataframe
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    nom_nom_assoc : str or callable
        If callable, a function which receives two `pd.Series` (and optionally a weight array) and returns a single number.
        If string, name of nominal-nominal (categorical-categorical) association to use.
        Options are 'cramer' for Cramer's V or `theil` for Theil's U. If 'theil',
        heat-map columns are the provided information (U = U(row|col)).
    num_num_assoc : str or callable
        If callable, a function which receives two `pd.Series` and returns a single number.
        If string, name of numerical-numerical association to use. Options are 'pearson'
        for Pearson's R, 'spearman' for Spearman's R.
    nom_num_assoc : str or callable
        If callable, a function which receives two `pd.Series` and returns a single number.
        If string, name of nominal-numerical association to use. Options are 'correlation_ratio'
        for correlation ratio
    n_jobs : int, optional
        the number of cores to use for the computation, by default -1
    handle_na : str, optional
        either drop rows with na, fill na with 0 or do nothing, by default "drop"
    nom_nom_comb_list : list of 2-uple of strings
        Pairs of column names corresponding to the entries for nom_nom associations.
        If asymmetrical association, take care of providing an exhaustive list of column name pairs.
    num_num_comb_list : list of 2-uple of strings
        Pairs of column names corresponding to the entries for num_num associations
    nom_num_comb_list : list of 2-uple of strings
        Pairs of column names corresponding to the entries for nom_num associations

    Returns
    -------
    pd.DataFrame
        the association matrix
    """
    # sanity checks
    X, sample_weight = _check_association_input(X, sample_weight, handle_na)
    dtypes_dic = create_dtype_dict(X, dic_keys="dtypes")

    # Cramer's V only for categorical columns
    # in GLM supposed to be all the columns
    n_cat_cols = len(dtypes_dic["cat"])
    n_num_cols = len(dtypes_dic["num"])
    
    df_to_concat = []

    # num-num, NaNs already checked above, not repeating the process
    if n_num_cols >= 2:
        if callable(num_num_assoc):
            w_num_num = _callable_association_matrix_fn(
                assoc_fn=num_num_assoc,
                cols_comb=num_num_comb,
                kind="num-num",
                X=X,
                sample_weight=sample_weight,
                n_jobs=n_jobs,
            )
        else:
            w_num_num = wcorr_matrix(
                X, sample_weight, n_jobs, handle_na=None, method=num_num_assoc
            )
        df_to_concat.append(w_num_num) 

    # nom-num
    if (n_num_cols >= 1) and (n_cat_cols >= 1):
        if callable(nom_num_assoc):
            w_nom_num = _callable_association_matrix_fn(
                assoc_fn=nom_num_assoc,
                cols_comb=nom_num_comb,
                kind="nom-num",
                X=X,
                sample_weight=sample_weight,
                n_jobs=n_jobs,
            )
        else:
            w_nom_num = correlation_ratio_matrix(X, sample_weight, n_jobs, handle_na=None)
        df_to_concat.append(w_nom_num) 

    # nom-nom
    if n_cat_cols >= 2:
        if callable(nom_nom_assoc):
            w_nom_nom = _callable_association_matrix_fn(
                assoc_fn=nom_nom_assoc,
                cols_comb=nom_nom_comb,
                kind="nom-nom",
                X=X,
                sample_weight=sample_weight,
                n_jobs=n_jobs,
            )
        elif nom_nom_assoc == "cramer":
            w_nom_nom = cramer_v_matrix(X, sample_weight, n_jobs, handle_na=None)
        else:
            w_nom_nom = theils_u_matrix(X, sample_weight, n_jobs, handle_na=None)
        df_to_concat.append(w_nom_nom) 


    return pd.concat(df_to_concat, ignore_index=True)


def _callable_association_series_fn(
    assoc_fn, X, target, sample_weight=None, n_jobs=-1, kind="nom-nom"
):
    """_callable_association_series_fn private function, utility for computing association series
    for a callable custom association

    Parameters
    ----------
    assoc_fn : callable
        a function which receives two `pd.Series` (and optionally a weight array) and returns a single number
    X : array-like of shape (n_samples, n_features)
        predictor dataframe
    target : str or int
        the predictor name or index with which to compute association
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    n_jobs : int, optional
        the number of cores to use for the computation, by default -1
    kind : str
        kind of association, either 'num-num' or 'nom-nom' or 'nom-num'

    Returns
    -------
    pd.Series
        the association series

    Raises
    ------
    ValueError
        if kind is not 'num-num' or 'nom-nom' or 'nom-num'
    """
    col_dtypes_dic = create_dtype_dict(X)
    dtypes_dic = create_dtype_dict(X, dic_keys="dtypes")

    if kind == "nom-nom":
        if col_dtypes_dic[target] != "cat":
            raise TypeError("the target column is not categorical")
        nom_cols = dtypes_dic["cat"]
        if nom_cols:
            # define the number of cores
            n_jobs = (
                min(cpu_count(), len(nom_cols))
                if n_jobs == -1
                else min(cpu_count(), n_jobs)
            )
            # parallelize jobs
            _assoc_fn = partial(_compute_series, func_xyw=assoc_fn)
            return parallel_df(
                func=_assoc_fn,
                df=X[nom_cols],
                series=X[target],
                sample_weight=sample_weight,
                n_jobs=n_jobs,
            )
        else:
            return None

    elif kind == "nom-num":
        if col_dtypes_dic[target] == "cat":
            # if the target is categorical, pick only num predictors
            pred_list = dtypes_dic["num"]
        else:
            # if the target is numerical, the 2nd pred should be categorical
            pred_list = dtypes_dic["cat"]

        if pred_list:
            # define the number of cores
            n_jobs = (
                min(cpu_count(), len(pred_list))
                if n_jobs == -1
                else min(cpu_count(), n_jobs)
            )
            # parallelize jobs
            _assoc_fn = partial(_compute_series, func_xyw=assoc_fn)
            return parallel_df(
                func=_assoc_fn,
                df=X[pred_list],
                series=X[target],
                sample_weight=sample_weight,
                n_jobs=n_jobs,
            )
        else:
            return None

    elif kind == "num-num":
        num_cols = dtypes_dic["num"]
        if num_cols:
            # parallelize jobs
            _assoc_fn = partial(_compute_series, func_xyw=assoc_fn)
            return parallel_df(
                func=_assoc_fn,
                df=X[num_cols],
                series=X[target],
                sample_weight=sample_weight,
                n_jobs=n_jobs,
            )
        else:
            return None
    else:
        raise ValueError("kind can be 'num-num' or 'nom-num' or 'nom-nom'")


def _callable_association_matrix_fn(
    assoc_fn, X, sample_weight=None, n_jobs=-1, kind="nom-nom", cols_comb=None
):
    """_callable_association_matrix_fn private function, utility for computing association matrix
    for a callable custom association

    Parameters
    ----------
    assoc_fn : callable
        a function which receives two `pd.Series` (and optionally a weight array) and returns a single number
    X : array-like of shape (n_samples, n_features)
        predictor dataframe
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    n_jobs : int, optional
        the number of cores to use for the computation, by default -1
    kind : str
        kind of association, either 'num-num' or 'nom-nom' or 'nom-num'
    cols_comb : list of 2-uple of str, optional
        combination of column names (list of 2-uples of strings)

    Returns
    -------
    pd.DataFrame
        the association matrix
    """
    dtypes_dic = create_dtype_dict(X, dic_keys="dtypes")

    if cols_comb is None:
        if kind == "num-num":
            selected_cols = dtypes_dic["num"]
        elif kind == "nom-nom":
            selected_cols = dtypes_dic["cat"]
        elif kind == "nom-num":
            cat_cols = dtypes_dic["cat"]
            num_cols = dtypes_dic["num"]
            if cat_cols and num_cols:
                # explicitely store the unique 2-combinations of column names
                # the first one should be the categorical predictor
                selected_cols = list(product(cat_cols, num_cols))
        else:
            selected_cols = None

        if selected_cols:
            # explicitely store the unique 2-combinations of column names
            cols_comb = [comb for comb in combinations(selected_cols, 2)]
            _assoc_fn = partial(_compute_matrix_entries, func_xyw=assoc_fn)
            assoc = parallel_matrix_entries(
                func=_assoc_fn,
                df=X,
                comb_list=cols_comb,
                sample_weight=sample_weight,
                n_jobs=n_jobs,
            )

        else:
            assoc = None
    else:
        _assoc_fn = partial(_compute_matrix_entries, func_xyw=assoc_fn)
        assoc = parallel_matrix_entries(
            func=_assoc_fn,
            df=X,
            comb_list=cols_comb,
            sample_weight=sample_weight,
            n_jobs=n_jobs,
        )
    return assoc


################################
# Association predictor-target
################################


def f_oneway_weighted(*args):
    """
    Calculate the weighted F-statistic for one-way ANOVA (continuous target, categorical predictor).

    Parameters
    ----------
    X : array-like of shape (n_samples,)
        The predictor dataframe.
    y : array-like of shape (n_samples,)
        The target vector.
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None.

    Returns
    -------
    float
        The value of the F-statistic.

    Notes
    -----
    The F-statistic is calculated as:

    .. math::
        F(rf) = \\frac{\\sum_i (\\bar{Y}_{i \\bullet} - \\bar{Y})^2 / (K-1)}{\\sum_i \\sum_k (\\bar{Y}_{ij} - \\bar{Y}_{i\\bullet})^2 / (N - K)}
    """
    # how many levels (predictor)
    n_classes = len(args)
    # convert to float 2-uple d'array
    args = [as_float_array(a) for a in args]
    # compute the total weight per level
    weight_per_class = np.array([a[1].sum() for a in args])
    # total weight
    tot_weight = np.sum(weight_per_class)
    # weighted sum of squares
    ss_alldata = sum((a[1] * safe_sqr(a[0])).sum(axis=0) for a in args)
    # list of weighted sums
    sums_args = [np.asarray((a[0] * a[1]).sum(axis=0)) for a in args]
    square_of_sums_alldata = sum(sums_args) ** 2
    square_of_sums_args = [s**2 for s in sums_args]
    sstot = ss_alldata - square_of_sums_alldata / float(tot_weight)
    ssbn = 0.0
    for k, _ in enumerate(args):
        ssbn += square_of_sums_args[k] / weight_per_class[k]
    ssbn -= square_of_sums_alldata / float(tot_weight)
    sswn = sstot - ssbn
    dfbn = n_classes - 1
    dfwn = tot_weight - n_classes
    msb = ssbn / float(dfbn)
    msw = sswn / float(dfwn)
    constant_features_idx = np.where(msw == 0.0)[0]
    if np.nonzero(msb)[0].size != msb.size and constant_features_idx.size:
        warnings.warn("Features %s are constant." % constant_features_idx, UserWarning)
    f = msb / msw
    # flatten matrix to vector in sparse case
    f = np.asarray(f).ravel()
    return f


def f_cat_regression(x, y, sample_weight=None, as_frame=False):
    """f_cat_regression computes the weighted ANOVA F-value for the provided sample.
    (continuous target, categorical predictor)

    Parameters
    ----------
    x : pd.Series of shape (n_samples,)
        The predictor vector, the first categorical predictor
    y : pd.Series of shape (n_samples,)
        second categorical predictor, order doesn't matter, symmetrical association
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    as_frame: bool
        return output as a dataframe or a float

    Returns
    -------
    float
        value of the F-statistic
    """
    if sample_weight is None:
        sample_weight = np.ones_like(y)

    # one 2-uple per level of the categorical feature x
    args = [
        (
            y[safe_mask(y, x == k)],
            sample_weight[safe_mask(sample_weight, x == k)],
        )
        for k in np.unique(x)
    ]

    if as_frame:
        x_name = x.name if isinstance(x, pd.Series) else "var"
        y_name = y.name if isinstance(y, pd.Series) else "target"
        return pd.DataFrame(
            {"row": x_name, "col": y_name, "val": f_oneway_weighted(*args)[0]},
            index=[0],
        )
    else:
        return f_oneway_weighted(*args)[0]


def f_cat_regression_parallel(X, y, sample_weight=None, n_jobs=-1, handle_na="drop"):
    """f_cat_regression_parallel computes the weighted ANOVA F-value for the provided categorical predictors
    using parallelization of the code (continuous target, categorical predictor).

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)
        predictor dataframe
    y : array-like of shape (n_samples,)
        The target vector
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    n_jobs : int, optional
        the number of cores to use for the computation, by default -1
    handle_na : str, optional
        either drop rows with na, fill na with 0 or do nothing, by default "drop"

    Returns
    -------
    pd.Series
        the value of the F-statistic for each predictor
    """

    # Cramer's V only for categorical columns
    dtypes_dic = create_dtype_dict(X, dic_keys="dtypes")

    cat_cols = dtypes_dic["cat"]

    if not isinstance(y, pd.Series):
        y = pd.Series(y)
        y.name = "target"

    target = y.name
    X[target] = y.values
    # sanity checks
    X, sample_weight = _check_association_input(X, sample_weight, handle_na)

    y = X[target].copy()
    X = X.drop(target, axis=1)

    # define the number of cores
    n_jobs = min(cpu_count(), X.shape[1]) if n_jobs == -1 else min(cpu_count(), n_jobs)
    # parallelize jobs
    _f_stat_cat = partial(_compute_series, func_xyw=f_cat_regression)
    return parallel_df(
        func=_f_stat_cat,
        df=X[cat_cols],
        series=y,
        sample_weight=sample_weight,
        n_jobs=n_jobs,
    )


def f_cont_regression_parallel(
    X, y, sample_weight=None, n_jobs=-1, force_finite=True, handle_na="drop"
):
    """Univariate linear regression tests returning F-statistic.

    Quick linear model for testing the effect of a single regressor,
    sequentially for many regressors.
    This is done in 2 steps:
    1. The cross-correlation between each regressor and the target is computed using:
           E[(X[:, i] - mean(X[:, i])) * (y - mean(y))] / (std(X[:, i]) * std(y))
    2. It is converted to an F score ranks features in the same order if all the features
       are positively correlated with the target.
    Note that it is therefore recommended as a feature selection criterion to identify
    potentially predictive features for a downstream classifier, irrespective of
    the sign of the association with the target variable.

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)
        The predictor dataframe.
    y : array-like of shape (n_samples,)
        The target vector.
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None.
    n_jobs : int, optional
        The number of cores to use for the computation, by default -1.
    handle_na : str, optional
        Either drop rows with NaN, fill NaN with 0, or do nothing, by default "drop".
    force_finite : bool, optional
        Whether or not to force the F-statistics and associated p-values to
        be finite. There are two cases where the F-statistic is expected to not
        be finite:
        - when the target `y` or some features in `X` are constant. In this
          case, the Pearson's R correlation is not defined leading to obtain
          `np.nan` values in the F-statistic and p-value. When
          `force_finite=True`, the F-statistic is set to `0.0` and the
          associated p-value is set to `1.0`.
        - when a feature in `X` is perfectly correlated (or
          anti-correlated) with the target `y`. In this case, the F-statistic
          is expected to be `np.inf`. When `force_finite=True`, the F-statistic
          is set to `np.finfo(dtype).max`.

    Returns
    -------
    f_statistic : array-like of shape (n_features,)
        F-statistic for each feature.
    """
    if not isinstance(y, pd.Series):
        y = pd.Series(y)
        y.name = "target"

    target = y.name
    X[target] = y.values
    # sanity checks
    X, sample_weight = _check_association_input(X, sample_weight, handle_na)

    correlation_coefficient = wcorr_series(X, target, sample_weight, n_jobs, handle_na)

    deg_of_freedom = y.size - 2
    corr_coef_squared = correlation_coefficient**2

    with np.errstate(divide="ignore", invalid="ignore"):
        f_statistic = corr_coef_squared / (1 - corr_coef_squared) * deg_of_freedom

    if force_finite and not np.isfinite(f_statistic).all():
        # case where there is a perfect (anti-)correlation
        # f-statistics can be set to the maximum and p-values to zero
        mask_inf = np.isinf(f_statistic)
        f_statistic[mask_inf] = np.finfo(f_statistic.dtype).max
        # case where the target or some features are constant
        # f-statistics would be minimum and thus p-values large
        mask_nan = np.isnan(f_statistic)
        f_statistic[mask_nan] = 0.0

    return f_statistic.drop(labels=[target]).sort_values(ascending=False)


def f_stat_regression_parallel(
    X, y, sample_weight=None, n_jobs=-1, force_finite=True, handle_na="drop"
):
    """
    Compute the weighted explained variance for the provided categorical and numerical predictors using parallelization.

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)
        The predictor dataframe.
    y : array-like of shape (n_samples,)
        The target vector.
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None.
    n_jobs : int, optional
        The number of cores to use for the computation, by default -1.
    handle_na : str, optional
        Either drop rows with NA, fill NA with 0, or do nothing, by default "drop".
    force_finite : bool, optional
        Whether or not to force the F-statistics and associated p-values to be finite.
        There are two cases where the F-statistic is expected to not be finite:
        - When the target `y` or some features in `X` are constant. In this case,
          the Pearson's R correlation is not defined leading to obtain `np.nan`
          values in the F-statistic and p-value. When `force_finite=True`, the
          F-statistic is set to `0.0` and the associated p-value is set to `1.0`.
        - When a feature in `X` is perfectly correlated (or anti-correlated)
          with the target `y`. In this case, the F-statistic is expected to be `np.inf`.
          When `force_finite=True`, the F-statistic is set to `np.finfo(dtype).max`.

    Returns
    -------
    pd.Series
        The value of the F-statistic for each predictor.
    """
    f_stat_cont_series = f_cont_regression_parallel(
        X,
        y,
        sample_weight,
        n_jobs,
        force_finite=force_finite,
        handle_na=handle_na,
    )
    f_stat_cat_series = f_cat_regression_parallel(
        X, y, sample_weight, n_jobs, handle_na=handle_na
    )

    # normalize the scores
    # correlation coefficient varies in the range [-1, 1]
    # correlation ratio varies in the range [0, 1]
    # Cramer's V varies in the range [0, 1]
    # Theil's U varies in the range [0, 1]
    # F-statistic varies in the range [0, +inf] but both kind of F stat are not necessary on the same scale
    # in order to have a similar scale and compare both kind, one can studentize them
    if X.shape[1] > 1:
        f_stat_cont_series = (
            f_stat_cont_series - f_stat_cont_series.mean()
        ) / f_stat_cont_series.std()
        f_stat_cat_series = (
            f_stat_cat_series - f_stat_cat_series.mean()
        ) / f_stat_cat_series.std()

    return pd.concat([f_stat_cont_series, f_stat_cat_series]).sort_values(
        ascending=False
    )


def f_cont_classification(x, y, sample_weight=None, as_frame=False):
    """f_cont_classification computes the weighted ANOVA F-value for the provided sample.
    Categorical target, continuous predictor.

    Parameters
    ----------
    x : pd.Series of shape (n_samples,)
        The predictor vector, the first categorical predictor
    y : pd.Series of shape (n_samples,)
        second categorical predictor, order doesn't matter, symmetrical association
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    as_frame: bool
        return output as a dataframe or a float

    Returns
    -------
    float :
        value of the F-statistic
    """
    if sample_weight is None:
        sample_weight = np.ones_like(y)

    # one 2-uple per level of the categorical target y, continuous predictor x
    args = [
        (
            x[safe_mask(x, y == k)],
            sample_weight[safe_mask(sample_weight, y == k)],
        )
        for k in np.unique(y)
    ]

    if as_frame:
        x_name = x.name if isinstance(x, pd.Series) else "var"
        y_name = y.name if isinstance(y, pd.Series) else "target"
        return pd.DataFrame(
            {"row": x_name, "col": y_name, "val": f_oneway_weighted(*args)[0]},
            index=[0],
        )
    else:
        return f_oneway_weighted(*args)[0]


def f_cont_classification_parallel(
    X, y, sample_weight=None, n_jobs=-1, handle_na="drop"
):
    """f_cont_classification_parallel computes the weighted ANOVA F-value
    for the provided categorical predictors using parallelization of the code.
    Categorical target, continuous predictor.

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)
        The set of regressors that will be tested sequentially
    y : array-like of shape (n_samples,)
        The target vector
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    n_jobs : int, optional
        the number of cores to use for the computation, by default -1
    handle_na : str, optional
        either drop rows with na, fill na with 0 or do nothing, by default "drop"

    Returns
    -------
    pd.Series
        the value of the F-statistic for each predictor
    """
    dtypes_dic = create_dtype_dict(X, dic_keys="dtypes")

    num_cols = dtypes_dic["num"]

    if not isinstance(y, pd.Series):
        y = pd.Series(y)
        y.name = "target"

    target = y.name
    X[target] = y.values
    # sanity checks
    X, sample_weight = _check_association_input(X, sample_weight, handle_na)

    y = X[target].copy()
    X = X.drop(target, axis=1)

    # define the number of cores
    n_jobs = min(cpu_count(), X.shape[1]) if n_jobs == -1 else min(cpu_count(), n_jobs)
    # parallelize jobs
    _f_stat_cont_clf = partial(_compute_series, func_xyw=f_cont_classification)
    return parallel_df(
        func=_f_stat_cont_clf,
        df=X[num_cols],
        series=y,
        sample_weight=sample_weight,
        n_jobs=n_jobs,
    )


def f_cat_classification_parallel(
    X,
    y,
    sample_weight=None,
    n_jobs=-1,
    force_finite=True,
    handle_na="drop",
):
    """
    Univariate information dependence.

    It ranks features in the same order if all the features are positively correlated with the target.
    Note that it is therefore recommended as a feature selection criterion to identify
    potentially predictive features for a downstream classifier, irrespective of
    the sign of the association with the target variable.

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)
        The predictor dataframe.
    y : array-like of shape (n_samples,)
        The target vector.
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None.
    n_jobs : int, optional
        The number of cores to use for the computation, by default -1.
    handle_na : str, optional
        Either drop rows with NaN, fill NaN with 0, or do nothing, by default "drop".
    force_finite : bool, optional
        Whether or not to force the F-statistics and associated p-values to
        be finite. There are two cases where the F-statistic is expected to not
        be finite:
            - when the target `y` or some features in `X` are constant. In this
              case, the Pearson's R correlation is not defined leading to obtain
              `np.nan` values in the F-statistic and p-value. When
              `force_finite=True`, the F-statistic is set to `0.0` and the
              associated p-value is set to `1.0`.
            - when a feature in `X` is perfectly correlated (or
              anti-correlated) with the target `y`. In this case, the F-statistic
              is expected to be `np.inf`. When `force_finite=True`, the F-statistic
              is set to `np.finfo(dtype).max`.

    Returns
    -------
    f_statistic : array-like of shape (n_features,)
        F-statistic for each feature.
    """
    if not isinstance(y, pd.Series):
        y = pd.Series(y)
        y.name = "target"

    target = y.name
    X[target] = y.values
    # sanity checks
    X, sample_weight = _check_association_input(X, sample_weight, handle_na)

    deg_of_freedom = y.size - 2

    theils_u_coef = theils_u_series(X, target, sample_weight, n_jobs, handle_na)
    theils_u_coef_squared = theils_u_coef**2

    with np.errstate(divide="ignore", invalid="ignore"):
        f_statistic = (
            theils_u_coef_squared / (1 - theils_u_coef_squared) * deg_of_freedom
        )

    if force_finite and not np.isfinite(f_statistic).all():
        # case where there is a perfect (anti-)correlation
        # f-statistics can be set to the maximum and p-values to zero
        mask_inf = np.isinf(f_statistic)
        f_statistic[mask_inf] = np.finfo(f_statistic.dtype).max
        # case where the target or some features are constant
        # f-statistics would be minimum and thus p-values large
        mask_nan = np.isnan(f_statistic)
        f_statistic[mask_nan] = 0.0

    return f_statistic.drop(labels=[target]).sort_values(ascending=False)


def f_stat_classification_parallel(
    X, y, sample_weight=None, n_jobs=-1, force_finite=True, handle_na="drop"
):
    """
    Compute the weighted ANOVA F-value for the provided categorical and numerical predictors using parallelization.

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)
        The predictor dataframe.
    y : array-like of shape (n_samples,)
        The target vector.
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None.
    n_jobs : int, optional
        The number of cores to use for the computation, by default -1.
    handle_na : str, optional
        Either drop rows with NA, fill NA with 0, or do nothing, by default "drop".
    force_finite : bool, optional
        Whether or not to force the F-statistics and associated p-values to be finite.
        There are two cases where the F-statistic is expected to not be finite:
        - When the target `y` or some features in `X` are constant. In this case,
          the Pearson's R correlation is not defined leading to obtain `np.nan`
          values in the F-statistic and p-value. When `force_finite=True`, the
          F-statistic is set to `0.0` and the associated p-value is set to `1.0`.
        - When a feature in `X` is perfectly correlated (or anti-correlated)
          with the target `y`. In this case, the F-statistic is expected to be `np.inf`.
          When `force_finite=True`, the F-statistic is set to `np.finfo(dtype).max`.

    Returns
    -------
    pd.Series
        The value of the F-statistic for each predictor.
    """
    f_stat_cont_series = f_cont_classification_parallel(
        X, y, sample_weight, n_jobs, handle_na=handle_na
    )
    f_stat_cat_series = f_cat_classification_parallel(
        X,
        y,
        sample_weight,
        n_jobs,
        handle_na=handle_na,
        force_finite=force_finite,
    )

    # normalize the scores
    # correlation ratio varies in the range [0, 1]
    # Cramer's V varies in the range [0, 1]
    # Theil's U varies in the range [0, 1]
    # F-statistic varies in the range [0, +inf]
    # Both kind of F stat are not necessarily on the same scale
    # one can studentize them
    if X.shape[1] > 1:
        f_stat_cont_series = (
            f_stat_cont_series - f_stat_cont_series.mean()
        ) / f_stat_cont_series.std()
        f_stat_cat_series = (
            f_stat_cat_series - f_stat_cat_series.mean()
        ) / f_stat_cat_series.std()

    return pd.concat([f_stat_cont_series, f_stat_cat_series]).sort_values(
        ascending=False
    )


############
# Utilities
############


def _check_association_input(X, sample_weight=None, handle_na="drop"):
    """_check_association_input private function. Check the inputs,
    convert X to a pd.DataFrame if needed, adds column names if non are provided.
    Check if the sample_weight is None or of the right dimensionality and handle NA
    according to the chosen methods (drop, fill, None).

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)
        predictor dataframe
    sample_weight : array-like of shape (n_samples,), optional
        The weight vector, by default None
    handle_na : str, optional
        either drop rows with na, fill na with 0 or do nothing, by default "drop"

    Returns
    -------
    tuple
        the dataframe and the sample weights

    Raises
    ------
    ValueError
        if sample_weight contains NA
    """

    if not isinstance(X, pd.DataFrame):
        X = pd.DataFrame(X, columns=[f"pred_{i}" for i in range(X.shape[1])])

    # sanity checks
    if sample_weight is None:
        sample_weight = np.ones(len(X))
    elif ~np.isfinite(sample_weight).all():
        raise ValueError("sample weights contains nans or nulls")

    if isinstance(sample_weight, pd.Series):
        sample_weight = sample_weight.to_numpy()

    single_value_columns_set = set()
    for c in X.columns:
        if X[c].nunique() == 1:
            single_value_columns_set.add(c)

    if single_value_columns_set:
        warnings.warn(
            f"{single_value_columns_set} columns have been removed (single unique values)"
        )

    # handle nans
    if handle_na is None:
        pass
    elif handle_na == "drop":
        # mask the na
        na_mask = (~X.isnull().any(axis=1)).values
        if na_mask.any():
            X, sample_weight = X.loc[na_mask, :], sample_weight[na_mask]
    else:
        X = X.fillna(0)
    return X, sample_weight


def is_list_of_str(str_list):
    """Raise an exception if ``str_list`` is not a list of strings
    Parameters
    ----------
    str_list : list
        to list to be tested

    Raises
    ------
    TypeError
        if ``str_list`` is not a ``list[str]``
    """
    if str_list is not None:
        if not (
            isinstance(str_list, list) and all(isinstance(s, str) for s in str_list)
        ):
            return False
        else:
            return True


def matrix_to_xy(df, columns=None, reset_index=False):
    """matrix_to_xy wide to long format of the association matrix

    Parameters
    ----------
    df : pd.DataFrame
        the wide format of the association matrix
    columns : list of str, optional
        list of column names, by default None
    reset_index : bool, optional
        wether to reset_index or not, by default False

    Returns
    -------
    pd.DataFrame
        the long format of the association matrix
    """
    bool_index = np.tril(np.ones(df.shape), 0).astype(bool)
    xy = (
        df.where(bool_index).stack().reset_index()
        if reset_index
        else df.where(bool_index).stack()
    )
    if reset_index:
        xy.columns = columns or ["row", "col", "val"]
    return xy


def xy_to_matrix(xy):
    """xy_to_matrix long to wide format of the association matrix

    Parameters
    ----------
    xy : pd.DataFrame
        the long format of the association matrix, 3 columns.

    Returns
    -------
    pd.DataFrame

    """
    xy = xy.pivot(index="row", columns="col").fillna(0)
    xy.columns = xy.columns.droplevel(0)
    return xy.rename_axis(None, axis=1).rename_axis(None, axis=0)


###############
# visualization
###############


def cluster_sq_matrix(sq_matrix, method="ward"):
    """
    Apply agglomerative clustering to sort a square correlation matrix.

    Parameters
    ----------
    sq_matrix : pd.DataFrame
        A square correlation matrix.
    method : str, optional
        The linkage method, by default "ward".

    Returns
    -------
    pd.DataFrame
        A sorted square matrix.

    Example
    -------
    >>> from some_module import association_matrix, cluster_sq_matrix

    >>> assoc = association_matrix(iris_df, plot=False)
    >>> assoc_clustered = cluster_sq_matrix(assoc, method="complete")
    """
    d = sch.distance.pdist(sq_matrix.values)
    L = sch.linkage(d, method=method)
    ind = sch.fcluster(L, 0.5 * d.max(), "distance")
    columns = [sq_matrix.columns.tolist()[i] for i in list((np.argsort(ind)))]
    sq_matrix = sq_matrix.reindex(columns, axis=1)
    sq_matrix = sq_matrix.reindex(columns, axis=0)
    return sq_matrix


def heatmap(
    data, row_labels, col_labels, ax=None, cbar_kw=None, cbarlabel="", **kwargs
):
    """heatmap Create a heatmap from a numpy array and two lists of labels.

    Parameters
    ----------
    data : array-like of shape (M, N)
        matrix to plot
    row_labels : array-like of shape (M,)
        labels for the rows
    col_labels : array-like of shape (N,)
        labels for the columns
    ax : matplotlib.axes.Axes, optional
        A `matplotlib.axes.Axes` instance to which the heatmap is plotted.  If
        not provided, use current axes or create a new one, by default None
    cbar_kw : dict, optional
         A dictionary with arguments to `matplotlib.Figure.colorbar`, by default None
    cbarlabel : str, optional
        The label for the colorbar, by default ""
    kwargs : dict, optional
        All other arguments are forwarded to `imshow`.

    Returns
    -------
    tuple
        imgshow and cbar objects
    """

    if ax is None:
        ax = plt.gca()

    if cbar_kw is None:
        cbar_kw = {}

    # Plot the heatmap
    im = ax.imshow(data, **kwargs)

    # Create colorbar
    divider = make_axes_locatable(ax)

    ax_cb = divider.append_axes("right", size="5%", pad=0.05)
    fig = ax.get_figure()
    fig.add_axes(ax_cb)

    cbar = ax.figure.colorbar(im, cax=ax_cb, **cbar_kw)
    cbar.ax.set_ylabel(cbarlabel, rotation=-90, va="bottom")

    # Show all ticks and label them with the respective list entries.
    ax.set_xticks(np.arange(data.shape[1]), labels=col_labels)
    ax.set_yticks(np.arange(data.shape[0]), labels=row_labels)

    # Let the horizontal axes labeling appear on top.
    ax.tick_params(top=False, bottom=True, labeltop=False, labelbottom=True)

    # Rotate the tick labels and set their alignment.
    plt.setp(ax.get_xticklabels(), rotation=90, ha="right", rotation_mode="anchor")

    # Turn spines off and create white grid.
    ax.spines[:].set_visible(False)

    ax.set_xticks(np.arange(data.shape[1] + 1) - 0.5, minor=True)
    ax.set_yticks(np.arange(data.shape[0] + 1) - 0.5, minor=True)
    ax.grid(which="minor", color="w", linestyle="-", linewidth=2)
    ax.tick_params(which="minor", bottom=False, left=False)

    return im, cbar


def annotate_heatmap(
    im,
    data=None,
    valfmt="{x:.2f}",
    textcolors=("black", "white"),
    threshold=None,
    **textkw,
):
    """annotate_heatmap annotates a heatmap

    Parameters
    ----------
    im : matplotlib.axes.Axes
        The AxesImage to be labeled
    data : array-like of shape (M, N), optional
        data to illustrate, if none is provided the function retrieves
        the array of the mlp obkect, by default None
    valfmt : str, optional
        annotation formating, by default "{x:.2f}"
    textcolors : tuple, optional
        A pair of colors.  The first is used for values below a threshold,
        the second for those above, by default ("black", "white")
    threshold : float, optional
        Value in data units according to which the colors from textcolors are
        applied.  If None (the default) uses the middle of the colormap as
        separation, by default None
    textkw : dict, optional
        All other arguments are forwarded to mpl annotation.

    Returns
    -------
    _type_
        _description_
    """

    if not isinstance(data, (list, np.ndarray)):
        data = im.get_array()

    # Normalize the threshold to the images color range.
    if threshold is not None:
        threshold = im.norm(threshold)
    else:
        threshold = im.norm(data.max()) / 2.0

    # Set default alignment to center, but allow it to be
    # overwritten by textkw.
    kw = dict(horizontalalignment="center", verticalalignment="center")
    kw.update(textkw)

    # Get the formatter in case a string is supplied
    if isinstance(valfmt, str):
        valfmt = matplotlib.ticker.StrMethodFormatter(valfmt)

    # Loop over the data and create a `Text` for each "pixel".
    # Change the text's color depending on the data.
    texts = []
    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            kw.update(color=textcolors[int(im.norm(data[i, j]) > threshold)])
            text = im.axes.text(j, i, valfmt(data[i, j], None), **kw)
            texts.append(text)

    return texts


def plot_association_matrix(
    assoc_mat,
    suffix_dic=None,
    ax=None,
    cmap="PuOr",
    cbarlabel=None,
    figsize=None,
    show=True,
    cbar_kw=None,
    imgshow_kw=None,
    annotate=False,
):
    """plot_association_matrix renders the sorted associations/correlation matrix.
    The sorting is done using hierarchical clustering,
    very like in seaborn or other packages.
    Categorical(nom): uncertainty coefficient & correlation ratio from 0 to 1.
    The uncertainty coefficient is assymmetrical, (approximating how much the elements on the
    left PROVIDE INFORMATION on elements in the row). Continuous(con): symmetrical numerical
    correlations (Pearson's) from -1 to 1

    Parameters
    ----------
    assoc_mat : pd.DataFrame
        the square association frame
    suffix_dic : Dict[str, str], optional
        dictionary of data type for adding suffixes to column names
        in the plotting utility for association matrix, by default None
    ax : matplotlib.axes.Axes, optional
        _description_, by default None
    cmap : str, optional
        the colormap. Please use a scientific colormap. See the ``scicomap`` package, by default "PuOr"
    cbarlabel : str, optional
        the colorbar label, by default None
    figsize : Tuple[float, float], optional
        figure size in inches, by default None
    show : bool, optional
        Whether or not to display the figure, by default True
    cbar_kw : Dict, optional
        colorbar kwargs, by default None
    imgshow_kw : Dict, optional
        imgshow kwargs, by default None
    annotate : bool
        Whether to annotate or not the colormap

    Returns
    -------
    matplotlib.figure and matplotlib.axes.Axes
        the figure and the axes
    """
    # default size if None
    if figsize is None:
        ncol = len(assoc_mat)
        figsize = (ncol / 2.5, ncol / 2.5)

    # provide default to the figure
    if ax is None:
        fig, ax = plt.subplots(figsize=figsize)
    else:
        fig = ax.get_figure()
    assoc_mat = cluster_sq_matrix(assoc_mat)

    # provide default to imshow
    if imgshow_kw is None:
        imgshow_kw = {"vmin": -1, "vmax": 1}

    # provide default to the colorbar
    if cbar_kw is None:
        cbar_kw = {"ticks": [-1, -0.5, 0, 0.5, 1]}

    # rename the columns for keeping track of num vs cat columns
    if suffix_dic is not None:
        rename_dic = {c: f"{c}_{suffix_dic[c]}" for c in assoc_mat.columns}
        assoc_mat = assoc_mat.rename(columns=rename_dic)
        assoc_mat = assoc_mat.rename(index=rename_dic)

    im, cbar = heatmap(
        assoc_mat.values,
        assoc_mat.columns,
        assoc_mat.columns,
        ax=ax,
        cmap=cmap,
        cbarlabel=cbarlabel,
        cbar_kw=cbar_kw,
        **imgshow_kw,
    )

    if annotate:
        texts = annotate_heatmap(im, valfmt="{x:.1f}", textcolors=("white", "black"))

    fig.tight_layout()
    if show:
        plt.show()
    return fig, ax


def plot_association_matrix_int(
    assoc_mat, suffix_dic=None, cmap="PuOr", figsize=(800, 600), cluster_matrix=True
):
    """Plot the interactive sorted associations/correlation matrix.
    The sorting is done using hierarchical clustering,
    very like in seaborn or other packages.
    Categorical(nom): uncertainty coefficient & correlation ratio from 0 to 1.
    The uncertainty coefficient is assymmetrical, (approximating how much the elements on the
    left PROVIDE INFORMATION on elements in the row). Continuous(con): symmetrical numerical
    correlations (Pearson's) from -1 to 1

    Parameters
    ----------
    assoc_mat : pd.DataFrame
        the square association frame
    suffix_dic : Dict[str, str], optional
        dictionary of data type for adding suffixes to column names
        in the plotting utility for association matrix, by default None
    cmap : str, optional
        the colormap. Please use a scientific colormap. See the ``scicomap`` package, by default "PuOr"
    figsize : Tuple[float, float], optional
        figure size in inches, by default None
    cluster_matrix : bool
        whether or not to cluster the square matrix, by default True

    Returns
    -------
    panel.Column
        the panel object
    """
    try:
        import holoviews as hv
    except ImportError:
        raise ImportError(
            "Holoviews is not installed. Please install it using 'pip install holoviews'."
        )

    try:
        import panel as pn
    except ImportError:
        raise ImportError(
            "Panel is not installed. Please install it using 'pip install panel'."
        )

    cmap = cmap if cmap is not None else "coolwarm"

    # rename the columns for keeping track of num vs cat columns
    if suffix_dic is not None:
        rename_dic = {c: f"{c}_{suffix_dic[c]}" for c in assoc_mat.columns}
        assoc_mat = assoc_mat.rename(columns=rename_dic)
        assoc_mat = assoc_mat.rename(index=rename_dic)

    if cluster_matrix:
        assoc_mat = cluster_sq_matrix(assoc_mat)

    heatmap = hv.HeatMap((assoc_mat.columns, assoc_mat.index, assoc_mat)).redim.range(
        z=(-1, 1)
    )

    heatmap.opts(
        tools=["tap", "hover"],
        height=figsize[1],
        width=figsize[0],
        toolbar="left",
        colorbar=True,
        cmap=cmap,
        fontsize={"title": 12, "ticks": 12, "minor_ticks": 12},
        xrotation=90,
        invert_xaxis=False,
        invert_yaxis=True,
        xlabel="",
        ylabel="",
    )
    title_str = "**Continuous (con) and Categorical (nom) Associations **"
    sub_title_str = (
        "*Categorical(nom): uncertainty coefficient & correlation ratio from 0 to 1. The uncertainty "
        "coefficient is assymmetrical, (approximating how much the elements on the "
        "left PROVIDE INFORMATION on elements in the row). Continuous(con): symmetrical numerical "
        "correlations (Pearson's) from -1 to 1*"
    )
    panel_layout = pn.Column(
        pn.pane.Markdown(title_str, align="start", style={"color": "#575757"}),  # bold
        pn.pane.Markdown(
            sub_title_str, align="start", style={"color": "#575757"}
        ),  # italic
        heatmap,
        background="#ebebeb",
    )

    gc.enable()
    del assoc_mat
    gc.collect()
    return panel_layout
