import numpy as np
from astropy.io import fits
import shutil
from sunpy.coordinates import sun
from skimage.transform import rotate
from astropy.time import Time
from astropy import units as u
from astropy.coordinates import AltAz, get_body, EarthLocation
from math import cos, sin, tan, atan, atan2, degrees, hypot

import logging

OVRO_LWA_LOCATION = EarthLocation(lat=37.23977727 * u.deg, lon=-118.2816667 * u.deg, height=1183 * u.m)


def get_ovro_lwa_location():
    """Return the OVRO-LWA EarthLocation without requiring remote Astropy site data."""
    try:
        return EarthLocation.of_site('OVRO')
    except Exception:
        return OVRO_LWA_LOCATION


def _beam_model_from_name(usebeam):
    try:
        from .beammodel import BeamModel, ConstantBeam, Memo178Beam, SineBeam, beam_model
    except ImportError:  # pragma: no cover - supports direct script execution.
        from beammodel import BeamModel, ConstantBeam, Memo178Beam, SineBeam, beam_model

    if isinstance(usebeam, BeamModel) or callable(usebeam):
        return usebeam
    normalized = str(usebeam).strip().lower().replace("_", "").replace("-", "")
    aliases = {
        "constantbeam": ConstantBeam,
        "sinebeam": SineBeam,
        "memo178beam": Memo178Beam,
        "lwa178beam": Memo178Beam,
    }
    if normalized in aliases:
        return aliases[normalized]()
    return beam_model(normalized)


def get_sun_altaz(obstime, location=None):
    """Return Sun azimuth and elevation in radians at the given location and time."""
    location = get_ovro_lwa_location() if location is None else location
    sun_coord = get_body('sun', obstime, location)
    altaz = sun_coord.transform_to(AltAz(obstime=obstime, location=location))
    return altaz.az.to(u.rad).value, altaz.alt.to(u.rad).value


def primary_beam_gain(frequency_hz, azimuth_rad, elevation_rad, usebeam="Memo178Beam"):
    """Return the scalar Stokes-I primary-beam power gain for a beam model."""
    beam = _beam_model_from_name(usebeam)
    jones = np.asarray(beam(frequency_hz, azimuth_rad, elevation_rad), dtype=np.complex128)
    power = np.real(np.trace(jones @ np.swapaxes(jones.conj(), -1, -2), axis1=-2, axis2=-1) / 2.0)
    return float(np.asarray(power))


def angdist(ra1, de1, ra2, de2):
    """
    Calculate angular distance using Vincenty equation (in radians).
    
    Args:
        ra1, de1: Right ascension and declination of first point
        ra2, de2: Right ascension and declination of second point
        
    Returns:
        float: Angular distance in radians
    """
    num1 = cos(de2) * sin(ra2 - ra1)
    num2 = cos(de1) * sin(de2) - sin(de1) * cos(de2) * cos(ra2 - ra1)
    denominator = sin(de1) * sin(de2) + cos(de1) * cos(de2) * cos(ra2 - ra1)
    return atan2(hypot(num1, num2), denominator)

def radec2hpc(ra, de, sun_ra, sun_de, sun_P):
    """
    Convert RA/Dec to helioprojective coordinates (all values in radians).
    
    Args:
        ra, de: Target right ascension and declination
        sun_ra, sun_de: Solar right ascension and declination
        sun_P: Solar P angle
        
    Returns:
        tuple: (rho, hpc_x, hpc_y) coordinates
    """
    rho = angdist(ra, de, sun_ra, sun_de)
    theta = atan2(sin(ra - sun_ra), 
                tan(de) * cos(sun_de) - sin(sun_de) * cos(ra - sun_ra))
    hpc_x = atan(-tan(rho) * sin(theta - sun_P))
    hpc_y = atan(tan(rho) * cos(theta - sun_P))
    return rho, hpc_x, hpc_y

def getSunEphem(reftimestr='', verbose=False):
    """
    Calculate solar ephemeris data using sunpy's direct coordinate functions.
    
    Args:
        reftime: Reference time (default: current time)
        verbose: Print detailed information if True
        
    Returns:
        dict: Solar ephemeris data
    """
    from datetime import datetime
    
    if reftimestr == '':
        start_time = datetime.now()
    else:
        start_time = datetime.strptime(reftimestr, '%Y-%m-%dT%H:%M:%S.%f')
    
    # Convert to astropy time
    obstime = Time(start_time)
    
    # Get coordinates using astropy
    location = get_ovro_lwa_location()
    phasecentre = get_body('sun', obstime, location)
    ra = phasecentre.ra.to(u.rad).value
    dec = phasecentre.dec.to(u.rad).value
    
    # Get solar angles from sunpy
    P = sun.P(obstime).to(u.rad).value
    B0 = sun.B0(obstime).to(u.rad).value
    L0 = sun.L0(obstime).to(u.rad).value
    
    # Get Earth-Sun distance and apparent radius
    dsun = sun.earth_distance(obstime).to(u.AU).value
    rapp = sun.angular_radius(obstime).to(u.rad).value
    
    if verbose:
        print('Local Solar Ephemeris Calculation')
        print('Date: ', start_time.strftime('%Y-%m-%d %H:%M:%S'))
        print(f'RA: {degrees(ra):.6f}°, Dec: {degrees(dec):.6f}°')
        print(f'Distance: {dsun:.6f} AU')
        print(f'P: {degrees(P):.6f}°, B0: {degrees(B0):.6f}°, L0: {degrees(L0):.6f}°')
        print(f'Apparent radius: {degrees(rapp)*3600.0:.2f} arcsec')
    
    return {
        't': start_time.strftime('%Y-%m-%dT%H:%M:%S.%f'),
        'ra': ra,
        'dec': dec,
        'dsun': dsun,
        'rrate': 0.0,
        'rapp': rapp,
        'P': P,
        'B0': B0,
        'L0': L0
    }

def fitsj2000tohelio(in_fits, out_fits=None, reftime="", toK=True,
        verbose=False, sclfactor=1.0, subregion=None,
        usebeam="Memo178Beam", beam_correction=True):
    """
    Convert a FITS image from J2000 to helioprojective coordinates.
    
    Args:
        in_fits (str): Input FITS file path
        out_fits (str): Output FITS file path
        reftime (str): Reference time (default: from FITS header)
        toK (bool): Convert data from Jy/beam to Kelvin if True
        usebeam: Beam model name or callable. Defaults to ``Memo178Beam``.
        beam_correction (bool): Divide image data by the solar primary-beam gain.
        verbose (bool): Print detailed information if True
    """
    # Constants
    JPL_AU = 149597870700.0  # meters
    JPL_RSUN = 696000000.0   # meters
    
    if out_fits is None:
        out_fits = in_fits.replace('.fits', '.helio.fits')

    # Copy input file to output location.
    shutil.copyfile(in_fits, out_fits)
    
    # Open the FITS file for updating
    hdul = fits.open(out_fits, mode="update")
    hdr = hdul[0].header
    data = hdul[0].data

    
    # Get observation time and solar ephemeris
    obstimestr = reftime if reftime else hdr["DATE-OBS"]
    ephemSun = getSunEphem(obstimestr, verbose=verbose)
    
    # Rotate image by solar P angle
    rotated_data = np.zeros_like(data)
    P_deg = np.degrees(ephemSun['P'])

    if len(data.shape) == 2:
        rotated_data = rotate(data.astype(np.float32), angle=P_deg, 
                            preserve_range=True, mode='constant', cval=np.nan)
    elif len(data.shape) == 4:
        for i in range(data.shape[0]):
            for j in range(data.shape[1]):
                rotated_data[i, j, :, :] = rotate(data[i, j, :, :].astype(np.float32),
                                                angle=P_deg, preserve_range=True,
                                                mode='constant', cval=np.nan)
    
    # Extract and convert coordinates
    crval1 = float(hdr['CRVAL1']) * u.Unit(hdr['CUNIT1'])
    crval2 = float(hdr['CRVAL2']) * u.Unit(hdr['CUNIT2'])
    cdelt1 = float(hdr['CDELT1']) * u.Unit(hdr['CUNIT1'])
    cdelt2 = float(hdr['CDELT2']) * u.Unit(hdr['CUNIT2'])
    
    # Convert to radians
    crval1 = crval1.to(u.rad).value
    crval2 = crval2.to(u.rad).value
    cdelt1 = cdelt1.to(u.rad).value
    cdelt2 = cdelt2.to(u.rad).value
    
    # Convert coordinates
    rho, crval1, crval2 = radec2hpc(crval1, crval2,
                                   sun_ra=ephemSun['ra'],
                                   sun_de=ephemSun['dec'],
                                   sun_P=ephemSun['P'])
    
    # Convert to appropriate units and prepare for header updates
    cdelt1 = -cdelt1  # Correct for different direction of RA and HGLN axes
    hpc_x = degrees(crval1) * 3600.0
    hpc_y = degrees(crval2) * 3600.0
    crota2 = 0.0  # Since we already rotated the image
    
    data = rotated_data

    # Apply subregion cropping if specified
    if subregion is not None:
        xmin, xmax, ymin, ymax = subregion
        if len(data.shape) == 2:
            data = data[ymin:ymax, xmin:xmax]
        elif len(data.shape) == 4:
            data = data[:, :, ymin:ymax, xmin:xmax]
        
        # Update reference pixel in header
        crpix1 = float(hdr.get('CRPIX1', 1))
        crpix2 = float(hdr.get('CRPIX2', 1))
        hdr['CRPIX1'] = crpix1 - xmin
        hdr['CRPIX2'] = crpix2 - ymin

    # update beam angle 
    if 'BPA' in hdr:
        bpa0 = hdr['BPA']
        bpa = (bpa0 - P_deg) % 360.0
        hdr['BPA'] = bpa
        logging.debug(f'Updating BPA: {bpa0} -> {bpa}, rotated by {P_deg:.2f} degrees')
    else:
        logging.debug(f'No BPA found in header, keeping original value')

    # Update header keywords
    header_updates = {
        'CRVAL1': hpc_x,
        'CRVAL2': hpc_y,
        'CUNIT1': 'arcsec',
        'CUNIT2': 'arcsec',
        'CDELT1': degrees(cdelt1) * 3600.0,
        'CDELT2': degrees(cdelt2) * 3600.0,
        'CTYPE1': 'HPLN-TAN',
        'CTYPE2': 'HPLT-TAN',
        'DSUN_REF': JPL_AU,
        'DSUN_OBS': ephemSun['dsun'] * JPL_AU,
        'RSUN_REF': JPL_RSUN,
        'RSUN_OBS': abs(degrees(ephemSun['rapp'])) * 3600.0,
        'HGLN_OBS': 0.0,
        'HGLT_OBS': degrees(ephemSun['B0']),
        'CRLN_OBS': degrees(ephemSun['L0']),
        'CRLT_OBS': degrees(ephemSun['B0']),
        'SOLAR_P': degrees(ephemSun['P']),
        'XCEN': hpc_x,
        'YCEN': hpc_y,
        'WCSNAME': 'Helioprojective-cartesian',
        'PC1_1': cos(crota2),
        'PC1_2': -sin(crota2) * cdelt2/cdelt1,
        'PC2_1': sin(crota2) * cdelt1/cdelt2,
        'PC2_2': cos(crota2),
    }
    
    # Update header
    for keyword, value in header_updates.items():
        if verbose:
            print(f'Updating {keyword}: {value}')
        hdr.set(keyword, value)
    
    # Update the data
    hdul[0].data = data
    
    # Convert from Jy/beam to Kelvin if requested
    if toK:
        if 'BUNIT' in hdr and hdr['BUNIT'] == 'K':
            if verbose:
                print('Data is already in Kelvin')
        else:
            if verbose:
                print('Converting data to Kelvin')
                
            if all(key in hdr for key in ['BMAJ', 'BMIN', 'CRVAL3']):
                bmaj = float(hdr['BMAJ']) * 3600.0  # Convert to arcsec
                bmin = float(hdr['BMIN']) * 3600.0  # Convert to arcsec
                freq = float(hdr['CRVAL3']) / 1e9  # Convert to GHz
                denom = bmaj * bmin * freq**2

                if not np.isfinite(denom) or denom <= 0:
                    print(
                        'Warning: Invalid beam/frequency metadata for Jy/beam to K '
                        f'conversion: BMAJ={hdr["BMAJ"]}, BMIN={hdr["BMIN"]}, '
                        f'CRVAL3={hdr["CRVAL3"]}; skipping conversion'
                    )
                else:
                    convJyb2K = 1.222e6 / denom

                    if verbose:
                        print(f'Beam major axis: {bmaj:.2f} arcsec')
                        print(f'Beam minor axis: {bmin:.2f} arcsec')
                        print(f'Frequency: {freq:.2f} GHz')

                    # Convert Jy/beam to K
                    hdul[0].data = hdul[0].data * convJyb2K
                    hdr['BUNIT'] = 'K'
            else:
                print('Warning: Missing required header keywords for Jy/beam to K conversion')

    if beam_correction:
        if 'CRVAL3' not in hdr:
            print('Warning: Missing CRVAL3 for primary beam correction')
        else:
            obstime = Time(obstimestr)
            sun_az, sun_el = get_sun_altaz(obstime)
            beam_gain = primary_beam_gain(hdr['CRVAL3'], sun_az, sun_el, usebeam=usebeam)
            if not np.isfinite(beam_gain) or beam_gain <= 0:
                print(f'Warning: Invalid primary beam gain {beam_gain}; skipping beam correction')
            else:
                if verbose:
                    print(f'Applying primary beam correction with {usebeam}')
                    print(f'Sun azimuth: {degrees(sun_az):.6f} deg')
                    print(f'Sun elevation: {degrees(sun_el):.6f} deg')
                    print(f'Primary beam gain: {beam_gain:.8g}')
                hdul[0].data = hdul[0].data / beam_gain
                hdr['PB_CORR'] = True
                hdr['PBMODEL'] = getattr(_beam_model_from_name(usebeam), '__class__', type(usebeam)).__name__
                hdr['PBGAIN'] = beam_gain
                hdr['SUN_AZ'] = degrees(sun_az)
                hdr['SUN_EL'] = degrees(sun_el)
    
    # multiply with scale factor
    hdul[0].data = hdul[0].data * sclfactor

    # Save and close
    hdul.flush()
    hdul.close()

    return out_fits


def fitsj2000tofits(*args, **kwargs):
    """Compatibility alias for :func:`fitsj2000tohelio`."""
    return fitsj2000tohelio(*args, **kwargs)

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python j2000_to_helio.py <input_fits> <output_fits> [reftime] [toK] [verbose]")
        sys.exit(1)
    
    in_fits = sys.argv[1]
    out_fits = sys.argv[2]
    reftime = sys.argv[3] if len(sys.argv) > 3 else ""
    toK = True if len(sys.argv) <= 4 or sys.argv[4].lower() == "true" else False
    verbose = True if len(sys.argv) > 5 and sys.argv[5].lower() == "true" else False
    
    fitsj2000tohelio(in_fits, out_fits, reftime, toK, verbose)
