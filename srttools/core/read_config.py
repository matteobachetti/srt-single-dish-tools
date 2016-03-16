"""Read the configuration file."""

from __future__ import (absolute_import, unicode_literals, division,
                        print_function)
import os
import glob
import warnings
import numpy as np
# For Python 2 and 3 compatibility
try:
    import configparser
except ImportError:
    import ConfigParser as configparser


SRT_tools_config_file = None
SRT_tools_config = None


def sample_config_file(fname='sample_config_file.ini'):
    """Create a sample config file, to be modified by hand."""
    string = """
[local]
; the directory where the analysis will be executed.
    workdir : .
; the root directory of the data repository.
    datadir : .

[analysis]
    projection : ARC
    interpolation : spline
    prefix : test_
    list_of_directories :
;;Two options: either a list of directories:
;        dir1
;        dir2
;; or a star symbol for all directories
;         *
    calibrator_directories :
; if left empty, calibrator scans are taken from list_of_directories when
; calculating light curves, and ignored when calculating images

;; Coordinates have to be specified in decimal degrees. ONLY use if different
;; from target coordinates!
;    reference_ra : 10.5
;    reference_dec : 5.3

;; Pixel size in arcminutes

    pixel_size : 1

;; Channels to save from RFI filtering. It might indicate known strong spectral
;; lines
    goodchans :

;; Percentage of channels to filter out for rough RFI filtering (Spectral data
;; only. PROBABLY OBSOLETE. AVOID IF UNSURE)
    filtering_factor : 0.
    """
    with open(fname, 'w') as fobj:
        print(string, file=fobj)
    return fname


def get_config_file():
    """Get the current config file."""
    return SRT_tools_config_file


def read_config(fname=None):
    """Read a config file and return a dictionary of all entries."""
    global SRT_tools_config_file, SRT_tools_config

    # --- If already read, use existing config ---

    if fname == SRT_tools_config_file and SRT_tools_config is not None:
        return SRT_tools_config

    if fname is None and SRT_tools_config is not None:
        return SRT_tools_config

    # ---------------------------------------------

    config_output = {}

    Config = configparser.ConfigParser()

    if fname is None:
        fname = sample_config_file()

    SRT_tools_config_file = fname
    Config.read(fname)

    # ---------- Set default values --------------------------------------

    config_output['projection'] = 'ARC'
    config_output['interpolation'] = 'linear'
    config_output['workdir'] = './'
    config_output['datadir'] = './'
    config_output['list_of_directories'] = '*'
    config_output['calibrator_directories'] = []
    config_output['pixel_size'] = '1'
    config_output['goodchans'] = None
    config_output['filtering_factor'] = '0'

    # --------------------------------------------------------------------

    # Read local information

    local_params = dict(Config.items('local'))

    config_output.update(local_params)
    if not config_output['workdir'].startswith('/'):
        config_output['workdir'] = os.path.abspath(os.path.join(os.path.split(fname)[0],
                                                                config_output['workdir']))

    if not config_output['datadir'].startswith('/'):
        config_output['datadir'] = os.path.abspath(os.path.join(os.path.split(fname)[0],
                                                                config_output['datadir']))

    # Read analysis information
    analysis_params = dict(Config.items('analysis'))

    config_output.update(analysis_params)

    try:
        config_output['list_of_directories'] = \
            [s for s in analysis_params['list_of_directories'].splitlines()
             if s.strip()]  # This last instruction eliminates blank lines
    except:
        warnings.warn("Invalid list_of_directories in config file")

    try:
        config_output['calibrator_directories'] = \
            [s for s in analysis_params['calibrator_directories'].splitlines()
             if s.strip()]  # This last instruction eliminates blank lines
    except:
        warnings.warn("Invalid calibrator_directories in config file")

    # If the list of directories is not specified, or if a '*' symbol is used,
    # use glob in the datadir to determine the list

    if config_output['list_of_directories'] in ([], ['*'], '*'):
        config_output['list_of_directories'] = \
            [os.path.split(f)[1]  # return name without path
             for f in glob.glob(os.path.join(config_output['datadir'], '*'))
             if os.path.isdir(f)]  # only if it's a directory

    config_output['pixel_size'] = np.radians(float(config_output['pixel_size']) * 60)

    if config_output['goodchans'] is not None:
        config_output['goodchans'] = \
            [int(n) for n in config_output['goodchans']]

    config_output['filtering_factor'] = \
        float(config_output['filtering_factor'])

    SRT_tools_config = config_output
    return config_output
