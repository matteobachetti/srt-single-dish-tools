# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
This packages contains affiliated package tests.
"""

def print_garbage(prefix):
    import tempfile
    string = ""
    for _ in range(5):
        garbage = '    ' + \
                  tempfile.NamedTemporaryFile(prefix=prefix).name[1:]
        string += garbage + '\n'
    return string


def sim_config_file(filename, add_garbage=False, prefix=None, label=""):
    """Create a sample config file, to be modified by hand."""
    string0 = f"""
[local]
workdir : .
datadir : .
productdir : test_image

[analysis]
projection : ARC
interpolation : spline
prefix : test_
list_of_directories :
    gauss_ra{label}
    gauss_dec{label}
    defective
"""
    string1 = """
calibrator_directories :
    calibration
"""
    string2 = """

skydip_directories :
    gauss_skydip

noise_threshold : 5

pixel_size : 0.8

[debugging]

debug_file_format : png

"""
    if prefix is None:
        prefix = os.getcwd()
    import tempfile
    string = string0
    if add_garbage:
        string += print_garbage(prefix)
    string += string1
    if add_garbage:
        string += print_garbage(prefix)
    string += string2

    with open(filename, 'w') as fobj:
        print(string, file=fobj)

    return string


def _2d_gauss(x, y, sigma=2.5 / 60.):
    """A Gaussian beam"""
    import numpy as np
    return np.exp(-(x ** 2 + y ** 2) / (2 * sigma**2))


def gauss_src_func(x, y):
    return 25 * _2d_gauss(x, y, sigma=2.5 / 60)


def source_scan_func(x):
    return 52 * _2d_gauss(x, 0, sigma=2.5 / 60)


def cal2_scan_func(x):
    return 132.1 * _2d_gauss(x, 0, sigma=2.5 / 60)


def prepare_simulated_data(simdir):
    from astropy import log
    from srttools.simulate import sim_crossscans, simulate_map
    from srttools.io import mkdir_p
    import numpy as np
    np.random.seed(1241347)

    # ************* Create calibrators *******************
    caldir = os.path.join(simdir, 'calibration')
    caldir2 = os.path.join(simdir, 'calibration2')
    caldir3 = os.path.join(simdir, 'calibration_bad')
    crossdir = os.path.join(simdir, 'crossscans')

    log.info('Fake calibrators: DummyCal, 1 Jy.')
    mkdir_p(caldir)
    sim_crossscans(5, caldir)
    log.info('Fake calibrators: DummyCal2, 1.321 Jy.')
    mkdir_p(caldir2)
    sim_crossscans(5, caldir2, srcname='DummyCal2',
                   scan_func=cal2_scan_func)
    log.info('Fake calibrators: DummyCal2, wrong flux 0.52 Jy.')
    mkdir_p(caldir3)
    sim_crossscans(1, caldir3, srcname='DummyCal2',
                   scan_func=source_scan_func)
    log.info('Fake cross scans: DummySrc, 0.52 Jy.')
    mkdir_p(crossdir)
    sim_crossscans(5, crossdir, srcname='DummySrc',
                   scan_func=source_scan_func)

    simulated_flux = 0.25

    # ************* Create large-ish map *******************

    obsdir_ra = os.path.join(simdir, 'gauss_ra')
    obsdir_dec = os.path.join(simdir, 'gauss_dec')
    mkdir_p(obsdir_ra)
    mkdir_p(obsdir_dec)
    log.info('Fake map: Point-like (but Gaussian beam shape), '
             '{} Jy.'.format(simulated_flux))

    simulate_map(count_map=gauss_src_func,
                 length_ra=30.,
                 length_dec=30.,
                 outdir=(obsdir_ra, obsdir_dec), mean_ra=180,
                 mean_dec=45, speed=1.5,
                 spacing=0.5, srcname='Dummy', channel_ratio=0.8,
                 baseline="flat")

    config_file = \
        os.path.abspath(os.path.join(simdir, 'test_config_sim.ini'))
    sim_config_file(config_file, add_garbage=True,
                    prefix="./")

    # ************* Create small-ish map *******************

    obsdir_ra = os.path.join(simdir, 'gauss_ra_small')
    obsdir_dec = os.path.join(simdir, 'gauss_dec_small')
    mkdir_p(obsdir_ra)
    mkdir_p(obsdir_dec)
    log.info('Fake map: Point-like (but Gaussian beam shape), '
             '{} Jy.'.format(simulated_flux))
    simulate_map(count_map=gauss_src_func,
                 length_ra=15.,
                 length_dec=15.,
                 outdir=(obsdir_ra, obsdir_dec), mean_ra=180,
                 mean_dec=45, speed=3,
                 spacing=1, srcname='Dummy', channel_ratio=0.8,
                 baseline="flat")

    config_file = \
        os.path.abspath(os.path.join(simdir, 'test_config_sim_small.ini'))
    sim_config_file(config_file, add_garbage=True,
                    prefix="./", label='_small')

import os

curdir = os.path.dirname(__file__)
datadir = os.path.join(curdir, 'data')
simdir = os.path.join(datadir, 'sim')

if not os.path.exists(simdir):
    prepare_simulated_data(simdir)
