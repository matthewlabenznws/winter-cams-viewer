/* Winter CAMs website configuration.
   Data comes from public AWS S3 HTTPS URLs. NEVER put AWS credentials here.
   This is the S3 base URL from the existing CAMs viewer; change it if you
   decide to use a separate winter bucket or CloudFront distribution. */
window.WINTER_CAMS_CONFIG = Object.freeze({
  dataBaseUrl: 'https://mtl-nwslbf-model-data.s3.us-east-2.amazonaws.com',
  prefix: 'runs/cams/rrfs_mpas/winter/',
  animationMs: 650
});
