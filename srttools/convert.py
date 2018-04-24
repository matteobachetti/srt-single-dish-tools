from __future__ import print_function, division
from astropy.io import fits
import astropy.units as u
from astropy.time import Time
from astropy.table import Table
import numpy as np
import copy
import warnings
import os
import glob

from .io import get_coords_from_altaz_offset, correct_offsets
from .io import get_rest_angle, observing_angle, locations
from .converters.mbfits import MBFITS_creator


def convert_to_complete_fitszilla(fname, outname):
    if outname == fname:
        raise ValueError('Files cannot have the same name')

    lchdulist = fits.open(fname)

    feed_input_data = lchdulist['FEED TABLE'].data
    xoffsets = feed_input_data['xOffset'] * u.rad
    yoffsets = feed_input_data['yOffset'] * u.rad
    # ----------- Extract generic observation information ------------------
    site = lchdulist[0].header['ANTENNA'].lower()
    location = locations[site]

    rest_angles = get_rest_angle(xoffsets, yoffsets)

    datahdu = lchdulist['DATA TABLE']
    data_table_data = Table(datahdu.data)

    new_table = Table()
    info_to_retrieve = \
        ['time', 'derot_angle', 'el', 'az', 'raj2000', 'decj2000']
    for info in info_to_retrieve:
        new_table[info.replace('j2000', '')] = data_table_data[info]

    el_save = new_table['el']
    az_save = new_table['az']
    derot_angle = new_table['derot_angle']
    el_save.unit = u.rad
    az_save.unit = u.rad
    derot_angle.unit = u.rad
    times = new_table['time']

    for i, (xoffset, yoffset) in enumerate(zip(xoffsets, yoffsets)):
        obs_angle = observing_angle(rest_angles[i], derot_angle)

        # offsets < 0.001 arcseconds: don't correct (usually feed 0)
        if np.abs(xoffset) < np.radians(0.001 / 60.) * u.rad and \
                np.abs(yoffset) < np.radians(0.001 / 60.) * u.rad:
            continue
        el = copy.deepcopy(el_save)
        az = copy.deepcopy(az_save)
        xoffs, yoffs = correct_offsets(obs_angle, xoffset, yoffset)
        obstimes = Time(times * u.day, format='mjd', scale='utc')

        # el and az are also changed inside this function (inplace is True)
        ra, dec = \
            get_coords_from_altaz_offset(obstimes, el, az, xoffs, yoffs,
                                         location=location, inplace=True)
        ra = fits.Column(array=ra, name='raj2000', format='1D')
        dec = fits.Column(array=dec, name='decj2000', format='1D')
        el = fits.Column(array=el, name='el', format='1D')
        az = fits.Column(array=az, name='az', format='1D')
        new_data_extension = \
            fits.BinTableHDU.from_columns([ra, dec, el, az])
        new_data_extension.name = 'Coord{}'.format(i)
        lchdulist.append(new_data_extension)

    lchdulist.writeto(outname + '.fits', overwrite=True)


def launch_convert_coords(name, label):
    allfiles = []
    if os.path.isdir(name):
        allfiles += glob.glob(os.path.join(name, '*.fits'))
    else:
        allfiles += [name]

    for fname in allfiles:
        if 'summary.fits' in fname:
            continue
        outroot = fname.replace('.fits', '_' + label)
        convert_to_complete_fitszilla(fname, outroot)


def launch_mbfits_creator(name, label, test=False):
    if not os.path.isdir(name):
        raise ValueError('Input for MBFITS conversion must be a directory.')
    name = name.rstrip('/')
    mbfits = MBFITS_creator(name + '_' + label, test=test)
    summary = os.path.join(name, 'summary.fits')
    if os.path.exists(summary):
        mbfits.fill_in_summary(summary)

    for fname in glob.glob(os.path.join(name, '*.fits')):
        if 'summary.fits' in fname:
            continue
        mbfits.add_subscan(fname)


def main_convert(args=None):
    import argparse

    description = ('Load a series of scans and convert them to various'
                   'formats')
    parser = argparse.ArgumentParser(description=description)

    parser.add_argument("files", nargs='*',
                        help="Single files to process or directories",
                        default=None, type=str)

    parser.add_argument("-f", "--format", type=str, default='fitsmod',
                        help='Format of output files (options: '
                             'mbfits, indicating MBFITS v. 1.65; '
                             'fitsmod (default), indicating a fitszilla with '
                             'converted coordinates for feed number *n* in '
                             'a separate COORDn extensions)')

    parser.add_argument("--test",
                        help="Only to be used in tests!",
                        action='store_true', default=False)

    args = parser.parse_args(args)

    for fname in args.files:
        if args.format == 'fitsmod':
            launch_convert_coords(fname, args.format)
        elif args.format == 'mbfits':
            launch_mbfits_creator(fname, args.format, test=args.test)
        else:
            warnings.warn('Unknown output format')
