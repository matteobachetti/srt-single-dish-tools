"""Scan class."""
import time
import pickle
import glob
import os
import warnings
import sys

from astropy import log
import astropy.units as u
import numpy as np
from scipy.signal import medfilt
from astropy.table import Table, Column
#from memory_profiler import profile

try:
    import matplotlib.pyplot as plt
    from matplotlib.gridspec import GridSpec
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

from .io import read_data, root_name, get_chan_columns, get_channel_feed
from .io import mkdir_p
from .read_config import read_config, get_config_file
from .fit import ref_mad, contiguous_regions
from .fit import baseline_rough, baseline_als, linear_fun
from .interactive_filter import select_data
from .utils import jit, vectorize, HAS_NUMBA

__all__ = ["Scan", "interpret_frequency_range", "clean_scan_using_variability",
           "list_scans"]


if HAS_NUMBA:
    @vectorize
    def normalize_angle_mpPI(angle):  # pragma: no cover
        """Normalize angle between minus pi and pi."""
        TWOPI = 2 * np.pi
        while angle > np.pi:
            angle -= TWOPI
        while angle < - np.pi:
            angle += TWOPI
        return angle
else:
    def normalize_angle_mpPI(angle):
        """Normalize angle between minus pi and pi."""
        angle = np.asarray(angle)
        TWOPI = 2 * np.pi
        gtpi = angle >= np.pi
        while np.any(gtpi):
            angle[gtpi] -= TWOPI
            gtpi = angle >= np.pi
        ltpi = angle < -np.pi
        while np.any(ltpi):
            angle[ltpi] += TWOPI
            ltpi = angle < -np.pi
        return angle


def product_path_from_file_name(fname, workdir='.', productdir=None):
    """
    Examples
    --------
    >>> curpath = os.path.abspath('.')
    >>> path, fname = product_path_from_file_name('ciao.ciao', workdir='.', productdir=None)
    >>> os.path.abspath(path) == curpath
    True
    >>> dumdir = os.path.join('bu', 'bla')
    >>> dumfile = os.path.join(dumdir, 'ciao.ciao')
    >>> path, fname = product_path_from_file_name(dumfile, workdir=dumdir, productdir=None)
    >>> os.path.abspath(path) == os.path.abspath(dumdir)
    True
    >>> path, fname = product_path_from_file_name(dumfile, workdir='bu', productdir='be')
    >>> os.path.abspath(path) == os.path.abspath(os.path.join('be', 'bla'))
    True
    """
    filedir, fn = os.path.split(fname)
    if filedir == '':
        filedir = os.path.abspath(os.curdir)
    if productdir is None:
        rootdir = filedir
    else:
        base = os.path.commonprefix([filedir, workdir])
        relpath = os.path.relpath(filedir, base)
        rootdir = os.path.join(productdir, relpath)

    return os.path.normpath(rootdir), fn


def angular_distance(angle0, angle1):
    """Absolute difference of angle, including wraps.

    Examples
    --------
    >>> dist = 0.1
    >>> a0 = 1.
    >>> a1 = 1. + dist
    >>> np.isclose(angular_distance(a0, a1), dist)
    True
    >>> a0 = -0.05
    >>> a1 = 0.05
    >>> np.isclose(angular_distance(a0, a1), dist)
    True
    >>> a0 += 2 * np.pi
    >>> np.isclose(angular_distance(a0, a1), dist)
    True
    >>> a1 += 6 * np.pi
    >>> np.isclose(angular_distance(a0, a1), dist)
    True
    >>> a0 = np.pi - 0.5 * dist
    >>> a1 = np.pi + 0.5 * dist
    >>> np.isclose(angular_distance(a0, a1), dist)
    True
    >>> a0 = 2 * np.pi - 0.5 * dist
    >>> a1 = 2 * np.pi + 0.5 * dist
    >>> np.isclose(angular_distance(a0, a1), dist)
    True
    >>> np.all(np.isclose(
    ...     angular_distance([0, np.pi, 2 * np.pi],
    ...                      np.asarray([0, np.pi, 2 * np.pi]) + dist),
    ...                   dist))
    True
    """
    angle0 = np.fmod(angle0, 2 * np.pi)
    angle1 = np.fmod(angle1, 2 * np.pi)

    diff = angle1 - angle0

    return np.abs(normalize_angle_mpPI(diff))


def _split_freq_splat(freqsplat):
    freqmin, freqmax = \
        [float(f) for f in freqsplat.split(':')]
    return freqmin, freqmax


def interpret_frequency_range(freqsplat, bandwidth, nbin):
    """Interpret the frequency range specified in freqsplat.

    Parameters
    ----------
    freqsplat : str
        Frequency specification. If None, it defaults to the interval 10%-90%
        of the bandwidth. If ':', it considers the full bandwidth. If 'f0:f1',
        where f0 and f1 are floats/ints, f0 and f1 are interpreted as start and
        end frequency in MHz, *referred to the local oscillator* (LO; e.g., if
        '100:400', at 6.9 GHz this will mean the interval 7.0-7.3 GHz)
    bandwidth : float
        The bandwidth in MHz
    nbin : int
        The number of bins in the spectrum

    Returns
    -------
    freqmin : float
        The minimum frequency in the band (ref. to LO), in MHz
    freqmax : float
        The maximum frequency in the band (ref. to LO), in MHz
    binmin : int
        The minimum spectral bin
    binmax : int
        The maximum spectral bin

    Examples
    --------
    >>> interpret_frequency_range(None, 1024, 512)
    (102.4, 921.6, 51, 459)
    >>> interpret_frequency_range('default', 1024, 512)
    (102.4, 921.6, 51, 459)
    >>> interpret_frequency_range(':', 1024, 512)
    (0, 1024, 0, 511)
    >>> interpret_frequency_range('all', 1024, 512)
    (0, 1024, 0, 511)
    >>> interpret_frequency_range('200:800', 1024, 512)
    (200.0, 800.0, 100, 399)
    """

    if freqsplat is None or freqsplat == 'default':
        freqmin, freqmax = bandwidth / 10, bandwidth * 0.9
    elif freqsplat in ['all', ':']:
        freqmin, freqmax = 0, bandwidth
    else:
        freqmin, freqmax = _split_freq_splat(freqsplat)

    binmin = int(nbin * freqmin / bandwidth)
    binmax = int(nbin * freqmax / bandwidth) - 1

    return freqmin, freqmax, binmin, binmax


def _clean_dyn_spec(dynamical_spectrum, bad_intervals):
    cleaned_dynamical_spectrum = dynamical_spectrum.copy()
    for b in bad_intervals:
        if b[0] == 0:
            fill_lc = np.array(dynamical_spectrum[:, b[1]])
        elif b[1] >= dynamical_spectrum.shape[1]:
            fill_lc = np.array(dynamical_spectrum[:, b[0]])
        else:
            previous = np.array(dynamical_spectrum[:, b[0] - 1])
            next_bin = np.array(dynamical_spectrum[:, b[1]])
            fill_lc = (previous + next_bin) / 2

        for bsub in range(b[0], np.min([b[1], dynamical_spectrum.shape[1]])):
            cleaned_dynamical_spectrum[:, bsub] = fill_lc
        if b[0] == 0 or b[1] >= dynamical_spectrum.shape[1]:
            continue
    return cleaned_dynamical_spectrum


class StatResults():
    meanspec = None
    spectral_var = None
    baseline = None
    mask = None
    allbins = None
    varimg = None
    thresh_low = None
    thresh_high = None
    freqmask = None
    freqmin = None
    freqmax = None
    wholemask = None
    bandwidth = None
    length = None
    df = None


class CleaningResults():
    lc = None
    freqmin = None
    freqmax = None
    mask = None


def object_or_pickle(obj):
    if isinstance(obj, str):
        with open(obj, 'rb') as fobj:
            return pickle.load(fobj)
    return obj


def pickle_or_not(results, filename, test_data, min_MB=50):
    if sys.getsizeof(test_data) > min_MB * 1e6:
        log.info("The data set is large. Using partial data dumps")
        with open(filename, 'wb') as fobj:
            pickle.dump(results, fobj)
            results = filename
    return results


# @profile
def _get_spectrum_stats(dynamical_spectrum, freqsplat, bandwidth,
                        smoothing_window, noise_threshold, good_mask=None,
                        length=1,
                        filename='stats.p'):

    results = StatResults()
    dynspec_len, nbin = dynamical_spectrum.shape
    # Calculate spectral variability curve

    meanspec = np.sum(dynamical_spectrum, axis=0) / dynspec_len
    df = bandwidth / len(meanspec)
    allbins = np.arange(len(meanspec)) * df
    # Mask frequencies -- avoid those excluded from splat

    freqmask = np.ones(len(meanspec), dtype=bool)
    freqmin, freqmax, binmin, binmax = \
        interpret_frequency_range(freqsplat, bandwidth, nbin)
    freqmask[0:binmin] = False
    freqmask[binmax:] = False
    results.freqmask = freqmask
    results.freqmin = freqmin
    results.freqmax = freqmax
    results.wholemask = freqmask
    results.allbins = allbins
    results.meanspec = meanspec
    results.df = df
    results.length = length

    if dynspec_len < 10:
        warnings.warn("Very few data in the dataset. "
                      "Skipping spectral filtering.")
        return results

    spectral_var = \
        np.sqrt(np.sum((dynamical_spectrum - meanspec) ** 2,
                       axis=0) / dynspec_len) / meanspec

    varimg = np.sqrt((dynamical_spectrum - meanspec) ** 2) / meanspec

    # Set up corrected spectral var

    mod_spectral_var = spectral_var.copy()
    mod_spectral_var[0:binmin] = spectral_var[binmin]
    mod_spectral_var[binmax:] = spectral_var[binmax]

    # Some statistical information on spectral var

    # median_spectral_var = np.median(mod_spectral_var[freqmask])
    stdref = ref_mad(mod_spectral_var[freqmask], 20)

    # Calculate baseline of spectral var ---------------
    # Empyrical formula, with no physical meaning

    smoothing_window_int = int(nbin * smoothing_window) // 2 * 2 + 1
    smoothing_window_int = np.max([smoothing_window_int, 11])
    baseline = medfilt(mod_spectral_var[binmin:binmax],
                       smoothing_window_int)

    baseline = \
        np.concatenate((np.zeros(binmin) + baseline[0],
                        baseline,
                        np.zeros(nbin - binmax) + baseline[-1]
                        ))

    # Set threshold
    threshold_high = baseline + noise_threshold * stdref
    mask = spectral_var < threshold_high
    threshold_low = baseline - noise_threshold * stdref
    mask = mask & (spectral_var > threshold_low)

    if not np.any(mask):
        warnings.warn("No good channels found. A problem with the data or "
                      "incorrect noise threshold?")
        return None

    results.bandwidth = bandwidth
    results.mask = mask
    results.spectral_var = spectral_var
    results.freqmask = freqmask
    results.varimg = varimg
    results.baseline = baseline
    results.thresh_low = threshold_low
    results.thresh_high = threshold_high
    results.wholemask = freqmask & mask
    if good_mask is None:
        good_mask = np.zeros_like(freqmask, dtype=bool)
    results.wholemask[good_mask] = 1

    results = pickle_or_not(results, filename, varimg)
    return results


def plot_light_curve(
        lc, length, outfile="out", label="",
        debug_file_format='pdf', dpi=100, info_string="Empty info string"):
    times = \
        length * np.arange(lc.size) / lc.size
    fig = plt.figure("{}_{}".format(outfile, label), figsize=(15, 15))
    plt.plot(times, lc)
    plt.xlabel('Time')
    plt.ylabel('Counts')
    plt.gca().text(0.05, 0.95, info_string, horizontalalignment='left',
                   verticalalignment='top',
                   transform=plt.gca().transAxes, fontsize=19)
    plt.savefig("{}_{}.{}".format(outfile, label, debug_file_format),
                dpi=dpi)
    plt.close(fig)


def plot_all_spectra(allbins, dynamical_spectrum, outfile="out", label="",
        debug_file_format='pdf', dpi=100,
        info_string="Empty info string", fig=None):

    if fig is None:
        fig = plt.figure("{}_{}".format(outfile, label), figsize=(15, 15))

    for i in dynamical_spectrum:
        plt.plot(allbins[1:], i[1:])

    meanspec = np.sum(dynamical_spectrum, axis=0) / dynamical_spectrum.shape[0]

    plt.plot(allbins[1:], meanspec[1:])
    plt.xlabel('Time')
    plt.ylabel('Counts')
    ax = plt.gca()
    ax.text(0.05, 0.95, info_string, horizontalalignment='left',
            verticalalignment='top',
            transform=ax.transAxes, fontsize=19)
    plt.savefig("{}_{}.{}".format(outfile, label, debug_file_format),
                dpi=dpi)
    plt.close(fig)


def _clean_spectrum(dynamical_spectrum, stat_file, length, filename):
    dynspec_len, nbin = dynamical_spectrum.shape

    # Calculate first light curve
    times = length * np.arange(dynspec_len) / dynspec_len

    spec_stats = object_or_pickle(stat_file)

    freqmask = spec_stats.freqmask
    freqmin = spec_stats.freqmin
    freqmax = spec_stats.freqmax

    wholemask = spec_stats.wholemask

    bad_intervals = contiguous_regions(np.logical_not(wholemask))

    # Calculate cleaned dynamical spectrum

    cleaned_dynamical_spectrum = \
        _clean_dyn_spec(dynamical_spectrum, bad_intervals)

    lc_corr = np.sum(cleaned_dynamical_spectrum[:, freqmask], axis=1)
    if len(lc_corr) > 10:
        lc_corr = baseline_als(times, lc_corr, outlier_purging=False)
    else:
        lc_corr -= np.median(lc_corr)

    results = CleaningResults()  # create empty object
    results.lc = lc_corr
    results.freqmin = freqmin * u.MHz
    results.freqmax = freqmax * u.MHz
    results.mask = wholemask
    results.dynspec =  cleaned_dynamical_spectrum

    results = pickle_or_not(results, filename, cleaned_dynamical_spectrum)
    return results


def plot_spectrum_cleaning_results(
        cleaning_res_file, spec_stats_file, dynamical_spectrum,
        outfile="out", label="", debug_file_format='pdf', dpi=100,
        info_string="Empty info string"):
    # Now, PLOT IT ALL --------------------------------
    # Prepare subplots

    cleaning_results = object_or_pickle(cleaning_res_file)
    lc_corr = cleaning_results.lc
    dynspec_len = dynamical_spectrum.shape[0]
    cleaned_dynamical_spectrum = cleaning_results.dynspec

    spec_stats = object_or_pickle(spec_stats_file)

    freqmask = spec_stats.freqmask
    wholemask = spec_stats.wholemask
    mask = spec_stats.mask
    baseline = spec_stats.baseline
    varimg = spec_stats.varimg
    thresh_high = spec_stats.thresh_high
    thresh_low = spec_stats.thresh_low
    spectral_var = spec_stats.spectral_var
    bandwidth = spec_stats.bandwidth
    length = spec_stats.length
    allbins = spec_stats.allbins
    freqmin = spec_stats.freqmin
    freqmax = spec_stats.freqmax
    df = spec_stats.df

    bad_intervals = contiguous_regions(np.logical_not(wholemask))

    times = length * np.arange(dynspec_len) / dynspec_len
    lc = np.sum(dynamical_spectrum, axis=1)
    if len(lc) > 10:
        lc = baseline_als(times, lc)
    else:
        lc -= np.median(lc)
    lcbins = np.arange(lc.size)

    # Calculate frequency-masked lc
    lc_masked = np.sum(dynamical_spectrum[:, freqmask], axis=1)
    # lc_masked = baseline_als(times, lc_masked, outlier_purging=False)
    if len(lc_masked) > 10:
        lc_masked = baseline_als(times, lc_masked, outlier_purging=False)
    else:
        lc_masked -= np.median(lc_masked)

    meanspec = np.sum(dynamical_spectrum, axis=0) / dynspec_len
    cleaned_meanspec = \
        np.sum(cleaned_dynamical_spectrum,
               axis=0) / len(cleaned_dynamical_spectrum)
    cleaned_varimg = \
        np.sqrt((cleaned_dynamical_spectrum - cleaned_meanspec) ** 2 /
                cleaned_meanspec ** 2)
    cleaned_spectral_var = \
        np.sqrt(np.sum((cleaned_dynamical_spectrum - cleaned_meanspec) ** 2,
                       axis=0) / dynspec_len) / cleaned_meanspec

    mean_varimg = np.mean(cleaned_varimg[:, freqmask])
    std_varimg = np.std(cleaned_varimg[:, freqmask])

    bandwidth_unit = cleaning_results.freqmin.unit

    fig = plt.figure("{}_{}".format(outfile, label), figsize=(15, 15))

    if len(lc_corr) < 10:
        plot_all_spectra(allbins, dynamical_spectrum, outfile="out", label="",
                         debug_file_format='pdf', dpi=100,
                         info_string="Empty info string", fig=fig)
        return cleaning_results

    gs = GridSpec(4, 3, hspace=0, wspace=0,
                  height_ratios=(1.5, 1.5, 1.5, 1.5),
                  width_ratios=(3, 0., 1.2))
    ax_meanspec = plt.subplot(gs[0, 0])
    ax_dynspec = plt.subplot(gs[1, 0], sharex=ax_meanspec)
    ax_cleanspec = plt.subplot(gs[2, 0], sharex=ax_meanspec)
    ax_lc = plt.subplot(gs[1, 2], sharey=ax_dynspec)
    ax_cleanlc = plt.subplot(gs[2, 2], sharey=ax_dynspec, sharex=ax_lc)
    ax_var = plt.subplot(gs[3, 0], sharex=ax_meanspec)
    ax_text = plt.subplot(gs[0, 2])
    for ax in [ax_meanspec, ax_dynspec, ax_cleanspec, ax_var, ax_text]:
        ax.autoscale(False)

    ax_meanspec.set_ylabel('Counts')
    ax_dynspec.set_ylabel('Sample')
    ax_cleanspec.set_ylabel('Sample')
    ax_var.set_ylabel('r.m.s.')
    ax_var.set_xlabel('Frequency from LO ({})'.format(bandwidth_unit))
    ax_cleanlc.set_xlabel('Counts')

    # Plot mean spectrum

    ax_meanspec.plot(allbins[1:], meanspec[1:], label="Unfiltered")
    ax_meanspec.plot(allbins[wholemask], meanspec[wholemask],
                     label="Final mask")
    ax_meanspec.set_ylim([np.min(cleaned_meanspec),
                          np.max(cleaned_meanspec)])

    try:
        cmap = plt.get_cmap("magma")
    except Exception:
        cmap = plt.get_cmap("gnuplot2")
    ax_dynspec.imshow(varimg, origin="lower", aspect='auto',
                      cmap=cmap,
                      vmin=mean_varimg - 5 * std_varimg,
                      vmax=mean_varimg + 5 * std_varimg,
                      extent=(0, bandwidth,
                              0, varimg.shape[0]), interpolation='none')

    ax_cleanspec.imshow(cleaned_varimg, origin="lower", aspect='auto',
                        cmap=cmap,
                        vmin=mean_varimg - 5 * std_varimg,
                        vmax=mean_varimg + 5 * std_varimg,
                        extent=(0, bandwidth,
                                0, varimg.shape[0]), interpolation='none')

    # Plot variability

    ax_var.plot(allbins[1:], spectral_var[1:], label="Spectral rms")
    ax_var.plot(allbins[mask], spectral_var[mask])
    ax_var.plot(allbins, cleaned_spectral_var,
                zorder=10, color="k")

    if baseline is not None:
        ax_var.plot(allbins[1:], baseline[1:])
        ax_var.plot(allbins[1:], thresh_high[1:], color='r', lw=2)
        ax_var.plot(allbins[1:], thresh_low[1:], color='r', lw=2)
        diff_low = np.abs(baseline - thresh_low)
        diff_high = np.abs(thresh_high - baseline)
        minb = np.min(baseline[1:] - 2 * diff_low[1:])
        maxb = np.max(baseline[1:] + 2 * diff_high[1:])
        ax_var.set_ylim([minb, maxb])

    # Plot light curves

    ax_lc.plot(lc, lcbins, color="grey")
    ax_lc.plot(lc_masked, lcbins, color="b")
    ax_cleanlc.plot(lc_masked, lcbins, color="grey")
    ax_cleanlc.plot(lc_corr, lcbins, color="k")
    dlc = max(lc_corr) - min(lc_corr)
    ax_lc.set_xlim([np.min(lc_corr) - dlc / 10, max(lc_corr) + dlc / 10])

    # Indicate bad intervals

    for b in bad_intervals:
        maxsp = np.max(meanspec)
        ax_meanspec.plot(b * df, [maxsp] * 2, color='k', lw=2)
        middleimg = [varimg.shape[0] / 2]
        ax_dynspec.plot(b * df, [middleimg] * 2, color='k', lw=2)
        maxsp = np.max(spectral_var)
        ax_var.plot(b * df, [maxsp] * 2, color='k', lw=2)

    # Indicate freqmin and freqmax
    ax_var.set_xlim([0, allbins[-1]])

    ax_dynspec.axvline(freqmin)
    ax_dynspec.axvline(freqmax)
    ax_cleanspec.axvline(freqmin)
    ax_cleanspec.axvline(freqmax)
    ax_var.axvline(freqmin)
    ax_var.axvline(freqmax)
    ax_meanspec.axvline(freqmin)
    ax_meanspec.axvline(freqmax)

    ax_text.text(0.05, 0.95, info_string, horizontalalignment='left',
                 verticalalignment='top',
                 transform=ax_text.transAxes, fontsize=19)
    ax_text.axis("off")

    fig.tight_layout()

    plt.savefig(
        "{}_{}.{}".format(outfile, label, debug_file_format),
        dpi=dpi)
    plt.close(fig)


def clean_scan_using_variability(dynamical_spectrum, length, bandwidth,
                                 good_mask=None, freqsplat=None,
                                 noise_threshold=5., debug=True, plot=True,
                                 nofilt=False, outfile="out", label="",
                                 smoothing_window=0.05,
                                 debug_file_format='pdf',
                                 info_string="Empty info string", dpi=100):
    """Clean a spectroscopic scan using the difference of channel variability.

    From the dynamical spectrum, i.e. the list of spectra obtained in each
    sample of a scan, we calculate the rms variability of each frequency
    channel. This forms a sort of rms spectrum. We calculate the baseline of
    this spectrum, and all channels whose rms is above above noise_threshold
    times the reference median absolute deviation
    (:func:`srttools.fit.ref_mad`), calculated
    with a minimum window of 20 samples, are cut and assigned an interpolated
    value between the closest valid points.
    The baseline is calculated with
    :func:`srttools.fit.baseline_als`, using a lambda value depending on
    the number of channels, with a formula that has been shown to work in a few
    standard cases but might be modified in the future.

    Parameters
    ----------
    dynamical_spectrum : 2-d array
        Array of shape MxN, with M spectra of N elements each.
    length : float
        Duration in seconds of the scan (assumed to have constant sample time)
    bandwidth : float
        Bandwidth in MHz

    Other parameters
    ----------------
    good_mask : boolean array
        this mask specifies channels that should never be discarded as
        RFI, for example because they contain spectral lines
    freqsplat : str
        List of frequencies to be merged into one. See
        :func:`srttools.scan.interpret_frequency_range`
    noise_threshold : float
        The threshold, in sigmas, over which a given channel is
        considered noisy
    debug : bool
        Print out debugging information
    plot : bool
        Plot stuff
    nofilt : bool
        Do not filter noisy channels (set noise_threshold to 1e32)
    outfile : str
        Root file name for the diagnostics plots (outfile_label.png)
    label : str
        Label to append to the filename (outfile_label.png)
    smoothing_window : float
        Width of smoothing window, in fraction of spectral length

    Returns
    -------
    results : object
        The attributes of this object are:

        lc : array-like
            The cleaned light curve
        freqmin : float
            Minimum frequency in MHz, referred to local oscillator
        freqmax : float
            Maximum frequency in MHz, referred to local oscillator

    See Also
    --------
    srttools.fit.baseline_als
    srttools.fit.ref_mad
    """
    import gc
    rootdir, _ = os.path.split(outfile)
    if not os.path.exists(rootdir):
        mkdir_p(rootdir)

    try:
        bandwidth_unit = bandwidth.unit
        bandwidth = bandwidth.value
    except AttributeError:
        bandwidth_unit = u.MHz

    if len(dynamical_spectrum.shape) == 1:
        if not plot or not HAS_MPL:
            return None

        # Now, PLOT IT ALL --------------------------------
        # Prepare subplots
        plot_light_curve(
            dynamical_spectrum, length, outfile=outfile, label=label,
            debug_file_format=debug_file_format, dpi=dpi,
            info_string=info_string)
        return None

    dummy_file = f'{time.time() - 1570000000}_{np.random.randint(10**8)}.p'
    spec_stats_ = _get_spectrum_stats(
        dynamical_spectrum, freqsplat, bandwidth, smoothing_window,
        noise_threshold, length=length, good_mask=good_mask,
        filename=dummy_file)
    if spec_stats_ is None:
        return None

    cleaning_res_file = _clean_spectrum(
        dynamical_spectrum, spec_stats_, length,
        filename=dummy_file.replace('.p', '_cl.p'))
    gc.collect()

    if not plot or not HAS_MPL:
        log.debug("No plotting needs to be done.")
        return object_or_pickle(cleaning_res_file)

    plot_spectrum_cleaning_results(
        cleaning_res_file, spec_stats_, dynamical_spectrum,
        outfile=outfile, label=label,
        debug_file_format=debug_file_format, dpi=dpi,
        info_string=info_string)

    gc.collect()

    results = object_or_pickle(cleaning_res_file)

    for filename in [cleaning_res_file, dummy_file]:
        if isinstance(filename, str) and os.path.exists(filename):
            os.unlink(filename)

    return results


def frequency_filter(dynamical_spectrum, mask):
    """Clean a spectroscopic scan with a precooked mask.

    Parameters
    ----------
    dynamical_spectrum : 2-d array
        Array of shape MxN, with M spectra of N elements each.
    mask : boolean array
        this mask has False wherever the channel should be discarded

    Returns
    -------
    lc : array-like
        The cleaned light curve
    """
    if len(dynamical_spectrum.shape) == 1:
        return dynamical_spectrum

    lc_corr = np.sum(dynamical_spectrum[:, mask], axis=1)

    return lc_corr


def list_scans(datadir, dirlist):
    """List all scans contained in the directory listed in config."""
    scan_list = []

    for d in dirlist:
        list_of_files = glob.glob(os.path.join(datadir, d, '*.fits'))
        list_of_files += glob.glob(os.path.join(datadir, d, '*.fits[0-9]'))
        list_of_files += glob.glob(os.path.join(datadir, d, '*.fits[0-9][0-9]'))
        for f in list_of_files:
            if "summary.fits" in f:
                continue
            scan_list.append(f)
    return scan_list


class Scan(Table):
    """Class containing a single scan."""
    def __init__(self, data=None, config_file=None, norefilt=True,
                 interactive=False, nosave=False, debug=False, plot=False,
                 freqsplat=None, nofilt=False, nosub=False, avoid_regions=None,
                 save_spectrum=False, **kwargs):
        """Load a Scan object

        Parameters
        ----------
        data : str or None
            data can be one of the following: None, in which case an empty Scan
            object is created; a FITS or HDF5 archive, containing an on-the-fly
            or cross scan in one of the accepted formats; another :class:`srttools.scan.Scan` or
            :class:`astropy.table.Table` object
        config_file : str
            Config file containing the parameters for the images and the
            directories containing the image and calibration data
        norefilt : bool
            If an HDF5 archive is present with the same basename as the input
            FITS file, do not re-run the filtering (default True)
        freqsplat : str
            See :class:`srttools.scan.interpret_frequency_range`
        nofilt : bool
            See :class:`srttools.scan.clean_scan_using_variability`
        nosub : bool
            Do not run the baseline subtraction.

        Other Parameters
        ----------------
        kwargs : additional arguments
            These will be passed to :class:`astropy.table.Table` initializer
        """
        if config_file is None:
            config_file = get_config_file()

        if isinstance(data, Table):
            super().__init__(data, **kwargs)
        elif data is None:
            super().__init__(**kwargs)
            self.meta['config_file'] = config_file
            self.meta.update(read_config(self.meta['config_file']))
        else:  # if data is a filename
            self.meta['config_file'] = config_file
            self.meta.update(read_config(self.meta['config_file']))
            h5name = self.root_name(data) + '.hdf5'
            if os.path.exists(h5name) and norefilt:
                # but only if the modification time is later than the
                # original file (e.g. the fits file was not modified later)
                if os.path.getmtime(h5name) > os.path.getmtime(data):
                    data = h5name
            if debug:
                log.info('Loading file {}'.format(data))
            table = read_data(data)
            super().__init__(table, **kwargs)
            if not data.endswith('hdf5'):
                self.meta['filename'] = os.path.abspath(data)
            self.meta['config_file'] = config_file
            self.meta.update(read_config(self.meta['config_file']))

            self.check_order()

            self.clean_and_splat(freqsplat=freqsplat, nofilt=nofilt,
                                 noise_threshold=self.meta['noise_threshold'],
                                 debug=debug, save_spectrum=save_spectrum)

            if interactive:
                self.interactive_filter()

            if (('backsub' not in self.meta.keys() or
                    not self.meta['backsub'])) and not nosub:
                log.debug(f'Subtracting the baseline from '
                         f'{self.meta["filename"]}')
                try:
                    self.baseline_subtract(avoid_regions=avoid_regions,
                                           plot=plot)
                except Exception as e:
                    log.error("Baseline subtraction failed: {str(e)}")

            if not nosave:
                self.save()

    def chan_columns(self):
        """List columns containing samples."""
        return get_chan_columns(self)

    def get_info_string(self, ch):
        infostr = "Target: {}\n".format(self.meta['SOURCE'])
        infostr += "SubScan ID: {}\n".format(self.meta['SubScanID'])
        infostr += "Channel: {}\n".format(ch)
        infostr += "Mean RA: {:.2f} d\n".format(
            np.degrees(np.mean(self["ra"])))
        infostr += "Mean Dec: {:.2f} d\n".format(
            np.degrees(np.mean(self["dec"])))
        infostr += "Mean Az: {:.2f} d\n".format(
            np.degrees(np.mean(self["az"])))
        infostr += "Mean El: {:.2f} d\n".format(
            np.degrees(np.mean(self["el"])))
        infostr += "Receiver: {}\n".format(self.meta['receiver'])
        infostr += "Backend: {}\n".format(self.meta['backend'])
        infostr += "Frequency: {}\n".format(self[ch].meta['frequency'])
        infostr += "Bandwidth: {}\n".format(self[ch].meta['bandwidth'])
        return infostr

    def clean_and_splat(self, good_mask=None, freqsplat=None,
                        noise_threshold=5, debug=True, plot=True,
                        save_spectrum=False, nofilt=False):
        """Clean from RFI.

        Very rough now, it will become complicated eventually.

        Parameters
        ----------
        good_mask : boolean array
            this mask specifies intervals that should never be discarded as
            RFI, for example because they contain spectral lines
        freqsplat : str
            List of frequencies to be merged into one. See
            :func:`srttools.scan.interpret_frequency_range`
        noise_threshold : float
            The threshold, in sigmas, over which a given channel is
            considered noisy

        Returns
        -------
        masks : dictionary of boolean arrays
            this dictionary contains, for each detector/polarization, True
            values for good spectral channels, and False for bad channels.

        Other parameters
        ----------------
        save_spectrum : bool, default False
            Save the spectrum into a 'ChX_spec' column
        debug : bool, default True
            Be verbose
        plot : bool, default True
            Save images with quicklook information on single scans
        nofilt : bool
            Do not filter noisy channels (see
            :func:`clean_scan_using_variability`)
        """
        if debug:
            log.debug("Noise threshold: {}".format(noise_threshold))

        if self.meta['filtering_factor'] > 0.5:
            warnings.warn("Don't use filtering factors > 0.5. Skipping.")
            return

        chans = self.chan_columns()
        is_polarized = False
        mask = True
        for ic, ch in enumerate(chans):
            if '_Q' in ch or '_U' in ch:
                is_polarized = True
                continue

            results = \
                clean_scan_using_variability(
                    np.array(self[ch]), 86400 * (self['time'][-1] - self['time'][0]),
                    self[ch].meta['bandwidth'],
                    good_mask=good_mask,
                    freqsplat=freqsplat,
                    noise_threshold=noise_threshold,
                    debug=debug, plot=plot, nofilt=nofilt,
                    outfile=self.root_name(self.meta['filename']),
                    label="{}".format(ic),
                    smoothing_window=self.meta['smooth_window'],
                    debug_file_format=self.meta['debug_file_format'],
                    info_string=self.get_info_string(ch))

            if results is None:
                continue

            mask = mask & results.mask
            lc_corr = results.lc
            freqmin, freqmax = results.freqmin, results.freqmax

            self[ch + 'TEMP'] = Column(lc_corr)

            self[ch + 'TEMP'].meta.update(self[ch].meta)
            if save_spectrum:
                self[ch].name = ch + "_spec"
            else:
                self.remove_column(ch)
            self[ch + 'TEMP'].name = ch
            self[ch].meta['bandwidth'] = freqmax - freqmin

        if is_polarized:
            for ic, ch in enumerate(chans):
                if 'Q' not in ch and 'U' not in ch:
                    continue
                lc_corr = frequency_filter(self[ch], mask)

                self[ch + 'TEMP'] = Column(lc_corr)

                self[ch + 'TEMP'].meta.update(self[ch].meta)
                if save_spectrum:
                    self[ch].name = ch + "_spec"
                else:
                    self.remove_column(ch)
                self[ch + 'TEMP'].name = ch

    def baseline_subtract(self, kind='als', plot=False, avoid_regions=None,
                          **kwargs):
        """Subtract the baseline.

        Parameters
        ----------
        kind : str
            If 'als', use the Asymmetric Least Square fitting in
            :func:`srttools.fit.baseline_als`, using a very stiff baseline
            (lam=1e11). If 'rough', use
            :func:`srttools.fit.baseline_rough` instead.

        Other parameters
        ----------------
        plot : bool
            Plot diagnostic information in an image with the same basename as
            the fits file, an additional label corresponding to the channel, in
            PNG format.
        avoid_regions: [[r0_ra, r0_dec, r0_radius], [r1_ra, r1_dec, r1_radius]]
            Avoid these regions from the fit
        """
        for ch in self.chan_columns():
            if plot and HAS_MPL:
                fig = plt.figure("Sub" + ch)
                plt.plot(self['time'], self[ch] - np.min(self[ch]),
                         alpha=0.5)
            force_rough = False
            if 'Q' in ch or 'U' in ch:
                force_rough = True
            if len(self[ch]) < 10:
                self[ch] = self[ch] - np.median(self[ch])
                continue
            mask = np.ones(len(self[ch]), dtype=bool)
            feed = get_channel_feed(ch)
            if avoid_regions is not None:
                for r in avoid_regions:
                    ras = self['ra'][:, feed]
                    decs = self['dec'][:, feed]
                    ra_dist = angular_distance(ras, r[0])
                    dec_dist = angular_distance(decs, r[1])
                    dist = np.sqrt((ra_dist * np.cos(decs))**2 + dec_dist**2)
                    mask[dist < r[2]] = 0
            if kind == 'als' and not force_rough:
                self[ch] = baseline_als(self['time'], self[ch], mask=mask,
                                        **kwargs)
            elif kind == 'rough' or force_rough:
                self[ch] = baseline_rough(self['time'], self[ch], mask=mask)
            else:
                raise ValueError('Unknown baseline technique')

            if plot and HAS_MPL:
                plt.plot(self['time'], self[ch])
                out = root_name(self.meta['filename']) + '_{}.png'.format(ch)
                plt.savefig(out)
                plt.close(fig)
        self.meta['backsub'] = True

    def __repr__(self):
        """Give the print() function something to print."""
        reprstring = \
            '\n\n----Scan from file {0} ----\n'.format(self.meta['filename'])
        reprstring += repr(Table(self))
        return reprstring

    def write(self, fname, *args, **kwargs):
        """Same as Table.write, but adds path information for HDF5."""
        # log.info('Saving to {}'.format(fname))

        if fname.endswith('.hdf5'):
            kwargs['serialize_meta'] = kwargs.pop('serialize_meta', True)
            super(Scan, self).write(fname, *args, **kwargs)

        else:
            raise TypeError("Saving to anything else than HDF5 is not "
                            "supported at the moment")

    def check_order(self):
        """Check that times in a scan are monotonically increasing."""
        if not np.all(self['time'] == np.sort(self['time'])):
            raise ValueError('The order of times in the table is wrong')

    def interactive_filter(self, save=True, test=False):
        """Run the interactive filter."""
        for ch in self.chan_columns():
            # Temporary, waiting for AstroPy's metadata handling improvements
            feed = get_channel_feed(ch)

            selection = self['ra'][:, feed]

            ravar = np.abs(selection[-1] -
                           selection[0])

            selection = self['dec'][:, feed]
            decvar = np.abs(selection[-1] -
                            selection[0])

            # Choose if plotting by R.A. or Dec.
            if ravar > decvar:
                dim = 'ra'
            else:
                dim = 'dec'

            # ------- CALL INTERACTIVE FITTER ---------
            info = select_data(self[dim][:, feed], self[ch],
                               xlabel=dim, test=test)

            # -----------------------------------------

            if test:
                info['Ch']['zap'].xs = [-1e32, 1e32]
                info['Ch']['FLAG'] = True

            # Treat zapped intervals
            xs = info['Ch']['zap'].xs
            good = np.ones(len(self[dim]), dtype=bool)
            if len(xs) >= 2:
                intervals = list(zip(xs[:-1:2], xs[1::2]))
                for i in intervals:
                    good[np.logical_and(self[dim][:, feed] >= i[0],
                                        self[dim][:, feed] <= i[1])] = False
            self['{}-filt'.format(ch)] = good

            if len(info['Ch']['fitpars']) > 1:
                self[ch] -= linear_fun(self[dim][:, feed],
                                       *info['Ch']['fitpars'])
                self.meta['backsub'] = True

            if info['Ch']['FLAG']:
                self.meta['FLAG'] = True
        if save:
            self.save()
        self.meta['ifilt'] = True

    def root_name(self, fname):
        rootdir, fn = os.path.split(fname)
        if rootdir == '':
            rootdir = '.'

        if self.meta['productdir'] is not None:
            rootdir, fname = \
                product_path_from_file_name(fname, workdir=self.meta['workdir'],
                                            productdir=self.meta['productdir'])

        return root_name(os.path.join(rootdir, fn))

    def save(self, fname=None):
        """Call self.write with a default filename, or specify it."""
        if fname is None:
            fname = self.root_name(self.meta['filename']) + '.hdf5'

        rootdir, _ = os.path.split(fname)
        if not os.path.exists(rootdir):
            mkdir_p(rootdir)

        self.write(fname, overwrite=True)
