/* Winter CAMs viewer — a static GitHub Pages frontend for the RRFS-MPAS
   winter JSON + PNG manifest format produced by the GitHub/S3 pipeline.
   No server code, AWS credentials, or third-party JS packages are required. */
(() => {
  'use strict';

  const cfg = window.WINTER_CAMS_CONFIG || {};
  const BASE_URL = String(cfg.dataBaseUrl || '').replace(/\/+$/, '');
  const PREFIX = String(cfg.prefix || 'runs/cams/rrfs_mpas/winter/').replace(/^\/+|\/+$/g, '') + '/';
  const FRAME_DELAY_MS = Number.isFinite(cfg.animationMs) ? Math.max(150, cfg.animationMs) : 650;
  const DOMAINS = ['lbf', 'regional', 'central_plains'];
  const DOMAIN_LABELS = { lbf: 'LBF', regional: 'Regional', central_plains: 'Central Plains' };
  const PRODUCT_LABELS = {
    ptype_ref1km: '1-km Reflectivity + P-Type',
    ptype_refcomp: 'Composite Reflectivity + P-Type',
    qpf: 'Total QPF',
    asnow: 'Accumulated Snowfall',
    weasd: 'Snow-Water Equivalent',
    t2m: '2-m Temperature',
    t925: '925-mb Temperature', t850: '850-mb Temperature', t700: '700-mb Temperature',
    wspd925: '925-mb Wind Speed', wspd850: '850-mb Wind Speed', wspd700: '700-mb Wind Speed',
    tadv_925mb: '925-mb Temperature Advection',
    tadv_850mb: '850-mb Temperature Advection',
    tadv_700mb: '700-mb Temperature Advection',
    fgen_925mb: '925-mb Frontogenesis',
    fgen_850mb: '850-mb Frontogenesis',
    fgen_700mb: '700-mb Frontogenesis',
    thickness_mslp: '1000–500-mb Thickness + MSLP'
  };
  const PRODUCT_ORDER = Object.keys(PRODUCT_LABELS);
  const urlParams = new URLSearchParams(location.search);
  const initial = {
    cycle: urlParams.get('run'),
    hour: Number(urlParams.get('fhr')),
    product: urlParams.get('product'),
    domain: urlParams.get('domain'),
    view: urlParams.get('view'),
    rightProduct: urlParams.get('rightProduct')
  };
  const state = {
    entries: [],
    cycle: null,
    hour: null,
    product: initial.product || 'ptype_ref1km',
    rightProduct: initial.rightProduct || 'qpf',
    view: initial.view === 'compare_products' ? 'compare_products' : 'single',
    rightImageUrl: null,
    leftLoaded: false,
    rightLoaded: false,
    domain: DOMAINS.includes(initial.domain) ? initial.domain : 'lbf',
    manifest: null,
    imageUrl: null,
    timer: null,
    serial: 0,
    cacheToken: Date.now(),
    ready: false
  };
  const byId = id => document.getElementById(id);
  const els = {
    product: byId('productSelect'), run: byId('runSelect'), domain: byId('domainSelect'),
    view: byId('viewSelect'), rightProduct: byId('rightProductSelect'),
    rightBlock: byId('rightProductBlock'), rightPanel: byId('rightPanel'),
    rightImage: byId('rightPlotImage'), rightEmpty: byId('rightEmptyState'),
    rightEmptyTitle: byId('rightEmptyTitle'), rightEmptyDetails: byId('rightEmptyDetails'),
    rightTitle: byId('rightPanelTitle'), rightFullLink: byId('rightFullImageLink'),
    slider: byId('slider'), fhr: byId('fhrLabel'), tiles: byId('tiles'),
    status: byId('siteStatus'), validTime: byId('validTime'),
    play: byId('playBtn'), latest: byId('latestBtn'), refresh: byId('refreshBtn'),
    save: byId('savePngBtn'), image: byId('plotImage'), stage: byId('imageStage'),
    empty: byId('emptyState'), emptyTitle: byId('emptyTitle'), emptyDetails: byId('emptyDetails'),
    title: byId('plotTitle'), meta: byId('frameMeta'), caption: byId('mapCaption'),
    viewerTitle: byId('viewerTitle'),
    fullLink: byId('fullImageLink')
  };
  const manifestCache = new Map();

  const isCompare = () => state.view === 'compare_products';
  function setCompareView() {
    els.view.value = state.view;
    els.rightBlock.hidden = !isCompare();
    els.rightPanel.hidden = !isCompare();
    byId('plotWrap').classList.toggle('compare', isCompare());
    els.save.textContent = isCompare() ? 'Save Two-Panel PNG' : 'Save PNG';
  }
  function updateSaveButton() {
    els.save.disabled = !(state.leftLoaded && (!isCompare() || state.rightLoaded));
  }
  function dataUrl(relativePath) {
    // Only paths generated locally from the validated run metadata are accepted.
    return BASE_URL + '/' + PREFIX + relativePath;
  }
  function fmtHour(hour) { return 'F' + String(hour).padStart(3, '0'); }
  function fmtTile(hour) { return String(hour).padStart(3, '0'); }
  function productLabel(key) {
    return PRODUCT_LABELS[key] || String(key).replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }
  function utcTime(value) {
    const d = new Date(value);
    if (!Number.isFinite(d.getTime())) return 'Time unavailable';
    const y = d.getUTCFullYear();
    const mo = String(d.getUTCMonth() + 1).padStart(2, '0');
    const da = String(d.getUTCDate()).padStart(2, '0');
    const hh = String(d.getUTCHours()).padStart(2, '0');
    const mm = String(d.getUTCMinutes()).padStart(2, '0');
    return `${y}-${mo}-${da} ${hh}:${mm} UTC`;
  }
  function status(message, type = '') {
    els.status.textContent = message;
    els.status.className = 'status' + (type ? ' ' + type : '');
  }
  function empty(title, detail) {
    els.image.hidden = true;
    els.image.removeAttribute('src');
    els.image.alt = '';
    els.empty.hidden = false;
    els.emptyTitle.textContent = title;
    els.emptyDetails.textContent = detail;
    els.fullLink.hidden = true;
    els.fullLink.removeAttribute('href');
    state.leftLoaded = false;
    els.save.disabled = true;
    state.imageUrl = null;
  }
  function emptyRight(title, detail) {
    state.rightLoaded = false;
    state.rightImageUrl = null;
    els.rightImage.hidden = true;
    els.rightImage.removeAttribute('src');
    els.rightEmpty.hidden = false;
    els.rightEmptyTitle.textContent = title;
    els.rightEmptyDetails.textContent = detail;
    els.rightFullLink.hidden = true;
    els.rightFullLink.removeAttribute('href');
    updateSaveButton();
  }
  function setSelectOptions(select, pairs, selectedValue) {
    select.replaceChildren();
    pairs.forEach(([value, label]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    });
    if (pairs.some(([value]) => value === selectedValue)) select.value = selectedValue;
    else if (pairs.length) select.value = pairs[0][0];
    select.disabled = !pairs.length;
  }
  function entriesForCycle(cycle) {
    return state.entries.filter(entry => entry.cycle === cycle)
      .sort((a, b) => a.forecast_hour - b.forecast_hour);
  }
  function stopAnimation() {
    if (state.timer !== null) window.clearInterval(state.timer);
    state.timer = null;
    els.play.textContent = '▶ Play';
    els.play.setAttribute('aria-pressed', 'false');
  }
  function syncAddress() {
    if (!state.cycle || state.hour === null) return;
    const url = new URL(location.href);
    url.searchParams.set('run', state.cycle);
    url.searchParams.set('fhr', String(state.hour));
    url.searchParams.set('product', state.product);
    url.searchParams.set('domain', state.domain);
    url.searchParams.set('view', state.view);
    if (isCompare()) url.searchParams.set('rightProduct', state.rightProduct);
    else url.searchParams.delete('rightProduct');
    history.replaceState(null, '', url);
  }
  function renderHourControls() {
    const entries = entriesForCycle(state.cycle);
    const hours = entries.map(e => e.forecast_hour);
    const max = hours.length ? Math.max(...hours) : 0;
    els.slider.disabled = hours.length < 2;
    els.slider.min = '0';
    els.slider.max = String(max);
    els.slider.step = '1';
    els.slider.value = String(state.hour ?? 0);
    els.fhr.textContent = Number.isInteger(state.hour) ? fmtTile(state.hour) : '---';
    els.play.disabled = hours.length < 2;
    els.latest.disabled = !hours.length;
    els.tiles.replaceChildren();
    if (!hours.length) return;
    const available = new Set(hours);
    const fragment = document.createDocumentFragment();
    for (let hour = 0; hour <= max; hour++) {
      const exists = available.has(hour);
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = fmtTile(hour);
      btn.className = 'frame ' + (exists ? 'available' : 'missing') + (hour === state.hour ? ' active' : '');
      btn.disabled = !exists;
      btn.setAttribute('aria-label', fmtHour(hour) + (exists ? ' available' : ' not published'));
      if (hour === state.hour) btn.setAttribute('aria-current', 'true');
      if (exists) btn.addEventListener('click', () => chooseHour(hour));
      fragment.appendChild(btn);
    }
    els.tiles.appendChild(fragment);
  }
  function resetForNoRuns(message, level = 'warning') {
    ++state.serial;
    state.ready = false;
    state.entries = [];
    state.cycle = null;
    state.hour = null;
    state.manifest = null;
    setSelectOptions(els.run, [], null);
    setSelectOptions(els.product, [], null);
    setSelectOptions(els.rightProduct, [], null);
    emptyRight('Awaiting second product', 'Publish winter products to enable the two-panel view.');
    els.slider.disabled = true;
    els.fhr.textContent = '---';
    els.play.disabled = true;
    els.latest.disabled = true;
    els.tiles.replaceChildren();
    els.validTime.textContent = 'Waiting for first model run';
    els.title.textContent = 'RRFS-MPAS Winter Forecast';
    els.viewerTitle.textContent = 'RRFS-MPAS Winter Graphics';
    els.caption.textContent = 'RRFS-MPAS winter graphics · Awaiting first published run';
    empty('Awaiting RRFS-MPAS winter graphics',
      'Published forecast maps will appear here when the first model run is available on AWS S3.');
    status(message, level);
  }
  async function refreshRuns(preserveSelection = true) {
    stopAnimation();
    ++state.serial;
    status('Checking AWS S3 for available winter runs…');
    els.refresh.disabled = true;
    try {
      if (!BASE_URL.startsWith('https://')) throw new Error('Configure an HTTPS S3 base URL in assets/config.js');
      const response = await fetch(dataUrl('runs.json'), { cache: 'no-store' });
      if (response.status === 404) {
        resetForNoRuns('Website ready · No winter run index has been published to S3 yet.');
        return;
      }
      if (!response.ok) throw new Error(`S3 returned HTTP ${response.status} for runs.json`);
      const json = await response.json();
      if (!json || !Array.isArray(json.runs)) throw new Error('runs.json must contain a runs array');
      const unique = new Map();
      for (const item of json.runs) {
        if (!item || !/^\d{10}$/.test(String(item.cycle ?? ''))) continue;
        if (!Number.isInteger(item.forecast_hour) || item.forecast_hour < 0 || item.forecast_hour > 120) continue;
        if (item.manifest !== `${item.cycle}/manifest_f${String(item.forecast_hour).padStart(3, '0')}.json`) continue;
        unique.set(item.cycle + ':' + item.forecast_hour, item);
      }
      const entries = [...unique.values()].sort((a,b) => b.cycle.localeCompare(a.cycle) || a.forecast_hour-b.forecast_hour);
      if (!entries.length) {
        resetForNoRuns('Website ready · No valid winter forecast hours are published yet.');
        return;
      }
      const oldCycle = state.cycle;
      const oldHour = state.hour;
      state.entries = entries;
      state.ready = true;
      manifestCache.clear();
      state.cacheToken = Date.now();
      const cycles = [...new Set(entries.map(e => e.cycle))];
      let wantedCycle = preserveSelection ? oldCycle : (initial.cycle && cycles.includes(initial.cycle) ? initial.cycle : null);
      if (!wantedCycle || !cycles.includes(wantedCycle)) wantedCycle = cycles[0];
      state.cycle = wantedCycle;
      const runEntries = entriesForCycle(state.cycle);
      const desiredHour = preserveSelection ? oldHour : initial.hour;
      state.hour = runEntries.some(e => e.forecast_hour === desiredHour)
        ? desiredHour : runEntries[0].forecast_hour;
      setSelectOptions(els.run, cycles.map(c => [c, `${c.slice(0,4)}-${c.slice(4,6)}-${c.slice(6,8)} ${c.slice(8,10)}Z`]), state.cycle);
      renderHourControls();
      await loadCurrent();
    } catch (err) {
      resetForNoRuns('Cannot access the winter S3 index. Check the bucket URL, public access and CORS: ' + err.message, 'error');
    } finally {
      els.refresh.disabled = false;
    }
  }
  async function loadCurrent() {
    const selected = state.entries.find(e => e.cycle === state.cycle && e.forecast_hour === state.hour);
    if (!selected) return;
    const serial = ++state.serial;
    const key = selected.manifest;
    renderHourControls();
    els.fhr.textContent = fmtTile(state.hour);
    els.validTime.textContent = utcTime(selected.valid_time_utc);
    els.title.textContent = isCompare() ? productLabel(state.product) : `RRFS-MPAS | ${productLabel(state.product)}`;
    els.viewerTitle.textContent = isCompare() ? 'RRFS-MPAS Product Comparison' : `RRFS-MPAS ${productLabel(state.product)}`;
    empty('Loading winter forecast…', 'Retrieving the available forecast maps for this hour.');
    status('Loading ' + state.cycle + 'Z ' + fmtHour(state.hour) + '…');
    try {
      let manifest = manifestCache.get(key);
      if (!manifest) {
        const response = await fetch(dataUrl(key), { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status} reading ${key}`);
        manifest = await response.json();
        if (manifest.model !== 'rrfs_mpas' || manifest.cycle !== selected.cycle ||
            manifest.forecast_hour !== selected.forecast_hour ||
            !manifest.domains || typeof manifest.domains !== 'object') {
          throw new Error('Manifest does not match the selected model hour');
        }
        manifestCache.set(key, manifest);
      }
      if (serial !== state.serial) return; // Ignore old responses after controls change.
      state.manifest = manifest;
      showManifest(manifest);
      status(`Run ${state.cycle}Z · ${fmtHour(state.hour)} · ${utcTime(manifest.valid_time_utc)}`, 'success');
      syncAddress();
    } catch (err) {
      if (serial !== state.serial) return;
      state.manifest = null;
      empty('Forecast map unavailable', 'Could not load the published manifest for this forecast hour.');
      status('Map unavailable: ' + err.message, 'error');
    }
  }
  function showManifest(manifest) {
    const available = manifest.domains[state.domain] || {};
    const keys = Object.keys(available).filter(k => typeof available[k] === 'string');
    keys.sort((a, b) => {
      const ai = PRODUCT_ORDER.indexOf(a);
      const bi = PRODUCT_ORDER.indexOf(b);
      return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi) || a.localeCompare(b);
    });
    // The dropdown displays only products published for the selected hour/domain.
    setSelectOptions(els.product, keys.map(k => [k, productLabel(k)]), state.product);
    if (!keys.length) {
      empty('No maps for this domain', `No ${DOMAIN_LABELS[state.domain]} maps are published for ${fmtHour(state.hour)}.`);
      els.title.textContent = 'RRFS-MPAS | No published maps';
      els.caption.textContent = `Run ${state.cycle}Z · ${fmtHour(state.hour)} · ${DOMAIN_LABELS[state.domain]}`;
      return;
    }
    state.product = els.product.value;
    setSelectOptions(els.rightProduct, keys.map(k => [k, productLabel(k)]), state.rightProduct);
    if (isCompare() && keys.length > 1 && els.rightProduct.value === state.product) {
      const alternative = keys.find(k => k !== state.product);
      if (alternative) els.rightProduct.value = alternative;
    }
    state.rightProduct = els.rightProduct.value;
    const path = available[state.product];
    const expectedStart = state.cycle + '/' + state.domain + '/';
    if (typeof path !== 'string' || !path.startsWith(expectedStart) ||
        path.includes('..') || path.includes('\\') || path.includes('?') || path.includes('#') ||
        !/^\d{10}\/(lbf|regional|central_plains)\/[\w.-]+\.png$/.test(path)) {
      empty('Published image unavailable', 'The selected product contains an invalid or missing image path.');
      status('Invalid image path in published manifest.', 'error');
      return;
    }
    const url = dataUrl(path) + '?v=' + state.cacheToken;
    state.imageUrl = url;
    els.title.textContent = isCompare() ? productLabel(state.product) : `RRFS-MPAS | ${productLabel(state.product)}`;
    els.viewerTitle.textContent = isCompare() ? 'RRFS-MPAS Product Comparison' : `RRFS-MPAS ${productLabel(state.product)}`;
    els.meta.textContent = `${DOMAIN_LABELS[state.domain].toUpperCase()} · ${fmtHour(state.hour)}`;
    els.caption.textContent = `${state.cycle}Z run · Valid ${utcTime(manifest.valid_time_utc)} · ${DOMAIN_LABELS[state.domain]}`;
    state.leftLoaded = false;
    state.rightLoaded = false;
    updateSaveButton();
    els.image.hidden = true;
    els.image.alt = `${productLabel(state.product)} for ${DOMAIN_LABELS[state.domain]}, valid ${utcTime(manifest.valid_time_utc)}`;
    els.image.onload = () => {
      if (state.imageUrl !== url) return;
      els.empty.hidden = true;
      els.image.hidden = false;
      state.leftLoaded = true;
      updateSaveButton();
    };
    els.image.onerror = () => {
      if (state.imageUrl !== url) return;
      empty('PNG could not be loaded', 'Confirm the S3 object exists and can be read over HTTPS.');
      status('S3 image could not be loaded: ' + path, 'error');
    };
    els.fullLink.href = url;
    els.fullLink.hidden = false;
    els.empty.hidden = false;
    els.emptyTitle.textContent = 'Loading forecast image…';
    els.emptyDetails.textContent = 'The forecast image is being fetched from AWS S3.';
    els.image.src = url;
    if (isCompare()) {
      showRightProduct(available, manifest);
    } else {
      emptyRight('Awaiting second product', 'Switch to Compare Products to show a second panel.');
    }
  }
  function showRightProduct(available, manifest) {
    const path = available[state.rightProduct];
    els.rightTitle.textContent = productLabel(state.rightProduct);
    if (!path || !/^[0-9]{10}\/(lbf|regional|central_plains)\/[\w.-]+\.png$/.test(path) ||
        !path.startsWith(state.cycle + '/' + state.domain + '/') || path.includes('..')) {
      emptyRight('Second map unavailable', 'That product is not available for this domain and forecast hour.');
      return;
    }
    const url = dataUrl(path) + '?v=' + state.cacheToken;
    state.rightImageUrl = url;
    state.rightLoaded = false;
    updateSaveButton();
    els.rightImage.hidden = true;
    els.rightImage.alt = `${productLabel(state.rightProduct)} for ${DOMAIN_LABELS[state.domain]}, valid ${utcTime(manifest.valid_time_utc)}`;
    els.rightEmpty.hidden = false;
    els.rightEmptyTitle.textContent = 'Loading second product…';
    els.rightEmptyDetails.textContent = 'The comparison image is loading from AWS S3.';
    els.rightImage.onload = () => {
      if (state.rightImageUrl !== url) return;
      state.rightLoaded = true;
      els.rightImage.hidden = false;
      els.rightEmpty.hidden = true;
      updateSaveButton();
    };
    els.rightImage.onerror = () => {
      if (state.rightImageUrl !== url) return;
      emptyRight('Second map failed to load', 'Check the published PNG and its HTTPS permissions.');
    };
    els.rightFullLink.href = url;
    els.rightFullLink.hidden = false;
    els.rightImage.src = url;
  }
  function chooseHour(hour) {
    if (!state.entries.some(e => e.cycle === state.cycle && e.forecast_hour === hour)) return;
    state.hour = hour;
    loadCurrent();
  }
  function startAnimation() {
    const entries = entriesForCycle(state.cycle);
    if (entries.length < 2) return;
    const hours = entries.map(e => e.forecast_hour);
    stopAnimation();
    els.play.textContent = 'Ⅱ Pause';
    els.play.setAttribute('aria-pressed', 'true');
    state.timer = window.setInterval(() => {
      const currentIndex = hours.indexOf(state.hour);
      chooseHour(hours[(currentIndex + 1 + hours.length) % hours.length]);
    }, FRAME_DELAY_MS);
  }

  setCompareView();
  els.view.addEventListener('change', () => {
    state.view = els.view.value;
    setCompareView();
    if (state.manifest) showManifest(state.manifest);
    syncAddress();
  });
  els.rightProduct.addEventListener('change', () => {
    state.rightProduct = els.rightProduct.value;
    if (state.manifest) showManifest(state.manifest);
    syncAddress();
  });
  els.domain.value = state.domain;
  els.domain.addEventListener('change', () => {
    state.domain = els.domain.value;
    if (state.manifest) { showManifest(state.manifest); syncAddress(); }
  });
  els.product.addEventListener('change', () => {
    state.product = els.product.value;
    if (state.manifest) { showManifest(state.manifest); syncAddress(); }
  });
  els.run.addEventListener('change', () => {
    stopAnimation();
    state.cycle = els.run.value;
    const entries = entriesForCycle(state.cycle);
    state.hour = entries.length ? entries[0].forecast_hour : null;
    state.manifest = null;
    if (entries.length) loadCurrent();
  });
  els.slider.addEventListener('input', () => {
    const wanted = Number(els.slider.value);
    const hours = entriesForCycle(state.cycle).map(e => e.forecast_hour);
    if (!hours.length) return;
    const nearest = hours.reduce((a,b) => Math.abs(b-wanted) < Math.abs(a-wanted) ? b : a);
    els.fhr.textContent = fmtTile(nearest);
  });
  els.slider.addEventListener('change', () => {
    stopAnimation();
    const wanted = Number(els.slider.value);
    const hours = entriesForCycle(state.cycle).map(e => e.forecast_hour);
    if (!hours.length) return;
    const nearest = hours.reduce((a,b) => Math.abs(b-wanted) < Math.abs(a-wanted) ? b : a);
    chooseHour(nearest);
  });
  els.play.addEventListener('click', () => state.timer ? stopAnimation() : startAnimation());
  els.latest.addEventListener('click', () => {
    stopAnimation();
    const latest = state.entries[0];
    if (!latest) return;
    state.cycle = latest.cycle;
    const sameCycle = entriesForCycle(state.cycle);
    state.hour = sameCycle[0].forecast_hour;
    els.run.value = state.cycle;
    loadCurrent();
  });
  els.refresh.addEventListener('click', () => refreshRuns(true));
  els.save.addEventListener('click', async () => {
    if (!state.imageUrl || !state.leftLoaded || (isCompare() && !state.rightLoaded)) return;
    const urls = isCompare() ? [state.imageUrl, state.rightImageUrl] : [state.imageUrl];
    els.save.disabled = true;
    const productNames = isCompare() ? `${state.product}_vs_${state.rightProduct}` : state.product;
    const filename = `rrfs_mpas_winter_${productNames}_${state.domain}_${state.cycle}_f${String(state.hour).padStart(3,'0')}.png`;
    try {
      const blobs = await Promise.all(urls.map(async url => {
        const response = await fetch(url);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const blob = await response.blob();
        if (!blob.type.startsWith('image/') && blob.type !== 'application/octet-stream') throw new Error('Unexpected file type');
        return blob;
      }));
      let output = blobs[0];
      if (isCompare()) {
        const images = await Promise.all(blobs.map(blob => createImageBitmap(blob)));
        try {
          const width = Math.max(...images.map(img => img.width));
          const height = Math.max(...images.map(img => img.height));
          const canvas = document.createElement('canvas');
          canvas.width = width * 2;
          canvas.height = height;
          const ctx = canvas.getContext('2d');
          if (!ctx) throw new Error('Canvas is unavailable');
          ctx.fillStyle = '#000';
          ctx.fillRect(0, 0, canvas.width, canvas.height);
          images.forEach((img, i) => {
            const scale = Math.min(width/img.width, height/img.height);
            const w = img.width*scale, h = img.height*scale;
            ctx.drawImage(img, width*i+(width-w)/2, (height-h)/2, w, h);
          });
          output = await new Promise((resolve,reject) => canvas.toBlob(b => b ? resolve(b) : reject(new Error('Canvas export failed')), 'image/png'));
        } finally { images.forEach(img => img.close()); }
      }
      const blobUrl = URL.createObjectURL(output);
      const a = document.createElement('a');
      a.href = blobUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.setTimeout(() => URL.revokeObjectURL(blobUrl), 2000);
      status(isCompare() ? 'Two-panel PNG download started.' : 'PNG download started.', 'success');
    } catch (err) {
      status('PNG saving needs AWS S3 CORS access. Open the original PNG links to save images separately.', 'warning');
    } finally {
      updateSaveButton();
    }
  });
  window.addEventListener('keydown', (event) => {
    if (event.target && ['INPUT','SELECT','TEXTAREA'].includes(event.target.tagName)) return;
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    const hours = entriesForCycle(state.cycle).map(e => e.forecast_hour);
    const idx = hours.indexOf(state.hour);
    if (idx === -1) return;
    const next = hours[idx + (event.key === 'ArrowRight' ? 1 : -1)];
    if (next === undefined) return;
    event.preventDefault();
    stopAnimation();
    chooseHour(next);
  });
  refreshRuns(false);
})();
