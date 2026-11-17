/* Winter CAMs public-data configuration for shared HRRR/operational RRFS/RRFS-MPAS viewer.
   Keep AWS credentials ONLY in GitHub Actions secrets, never on the website. */
window.WINTER_CAMS_CONFIG = Object.freeze({
  dataBaseUrl: 'https://mtl-nwslbf-model-data.s3.us-east-2.amazonaws.com',
  modelPrefixes: Object.freeze({
    rrfs_mpas: 'runs/cams/rrfs_mpas/winter/',
    hrrr: 'runs/cams/hrrr/winter/',
    rrfs: 'runs/cams/rrfs/winter/'
  }),
  // Backward-compatible default for existing scripts that still read prefix.
  prefix: 'runs/cams/rrfs_mpas/winter/',
  animationMs: 650
});
