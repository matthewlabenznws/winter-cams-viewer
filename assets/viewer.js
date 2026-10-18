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
    asnow: 'Accumulated Snowfall (Model)',
    snow10to1: '10:1 Snowfall Estimate',
    weasd: 'Snow-Water Equivalent',
    t2m: '2-m Temperature',
    t925: '925-mb Temperature', t850: '850-mb Temperature', t700: '700-mb Temperature',
    wspd925: '925-mb Wind Speed', wspd850: '850-mb Wind Speed', wspd700: '700-mb Wind Speed',
    tadv_925mb: '925-mb Temperature Advection',
    tadv_850mb: '850-mb Temperature Advection',
    tadv_700mb: '700-mb Temperature Advection',
    fgen_925mb: '925-mb Advection + Frontogenesis',
    fgen_850mb: '850-mb Advection + Frontogenesis',
    fgen_700mb: '700-mb Advection + Frontogenesis',
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
    rightProduct: urlParams.get('rightProduct'),
    thirdProduct: urlParams.get('thirdProduct'),
    fourthProduct: urlParams.get('fourthProduct')
  };
  const state = {
    entries: [],
    cycle: null,
    hour: null,
    product: initial.product || 'ptype_ref1km',
    rightProduct: initial.rightProduct || 'qpf',
    thirdProduct: initial.thirdProduct || 't2m',
    fourthProduct: initial.fourthProduct || 'fgen_850mb',
    thirdImageUrl: null, fourthImageUrl: null,
    thirdLoaded: false, fourthLoaded: false,
    initializedProducts: false,
    productCatalog: new Map(),
    view: ['compare_products', 'four_panel'].includes(initial.view) ? initial.view : 'single',
    rightImageUrl: null,
    leftLoaded: false,
    rightLoaded: false,
    domain: DOMAINS.includes(initial.domain) ? initial.domain : 'lbf',
    manifest: null,
    imageUrl: null,
    timer: null,
    playing: false,
    playGeneration: 0,
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
    thirdBlock: byId('thirdProductBlock'), thirdProduct: byId('thirdProductSelect'),
    thirdPanel: byId('thirdPanel'), thirdImage: byId('thirdPlotImage'),
    thirdEmpty: byId('thirdEmptyState'), thirdEmptyTitle: byId('thirdEmptyTitle'),
    thirdEmptyDetails: byId('thirdEmptyDetails'), thirdTitle: byId('thirdPanelTitle'),
    thirdFullLink: byId('thirdFullImageLink'),
    fourthBlock: byId('fourthProductBlock'), fourthProduct: byId('fourthProductSelect'),
    fourthPanel: byId('fourthPanel'), fourthImage: byId('fourthPlotImage'),
    fourthEmpty: byId('fourthEmptyState'), fourthEmptyTitle: byId('fourthEmptyTitle'),
    fourthEmptyDetails: byId('fourthEmptyDetails'), fourthTitle: byId('fourthPanelTitle'),
    fourthFullLink: byId('fourthFullImageLink'),
    slider: byId('slider'), fhr: byId('fhrLabel'), tiles: byId('tiles'),
    status: byId('siteStatus'), validTime: byId('validTime'),
    play: byId('playBtn'), latest: byId('latestBtn'), refresh: byId('refreshBtn'),
    save: byId('savePngBtn'), image: byId('plotImage'), stage: byId('imageStage'),
    empty: byId('emptyState'), emptyTitle: byId('emptyTitle'), emptyDetails: byId('emptyDetails'),
    title: byId('plotTitle'), meta: byId('frameMeta'), caption: byId('mapCaption'),
    viewerTitle: byId('viewerTitle'),
    fullLink: byId('fullImageLink')
  };
  // Four-panel viewing can use most of the display without cropping maps.
  // Keep the toggle in the forecast-hour controls so it remains accessible
  // when the model/product header is hidden in the larger view.
  const focusButton = document.createElement('button');
  focusButton.id = 'fourFocusBtn';
  focusButton.type = 'button';
  focusButton.textContent = 'Enlarge 4 Maps';
  focusButton.title = 'Hide extra controls to make all four maps larger on screen';
  focusButton.hidden = true;
  els.fhr.after(focusButton);
  let focusActive = false;
  function setFocusView(enabled) {
    focusActive = Boolean(enabled) && isFour();
    document.body.classList.toggle('four-focus', focusActive);
    focusButton.textContent = focusActive ? 'Exit Larger View' : 'Enlarge 4 Maps';
    focusButton.setAttribute('aria-pressed', String(focusActive));
    focusButton.title = focusActive
      ? 'Return to model and product selection controls'
      : 'Hide extra controls to make all four maps larger on screen';
    if (focusActive) window.scrollTo(0, 0);
    scheduleFourPanelFit();
  }
  focusButton.addEventListener('click', () => setFocusView(!focusActive));

  const manifestCache = new Map();
  // A small sliding window of PNGs makes playback smooth without keeping an
  // entire high-resolution model run in memory.
  const imageCache = new Map();
  const MAX_CACHED_IMAGES = 16;

  function preloadImage(url) {
    const cached = imageCache.get(url);
    if (cached) {
      imageCache.delete(url);
      imageCache.set(url, cached);
      return cached;
    }
    const promise = new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(url);
      img.onerror = () => reject(new Error('Forecast PNG could not be loaded from S3'));
      img.src = url;
    });
    imageCache.set(url, promise);
    while (imageCache.size > MAX_CACHED_IMAGES) {
      imageCache.delete(imageCache.keys().next().value);
    }
    promise.catch(() => {
      if (imageCache.get(url) === promise) imageCache.delete(url);
    });
    return promise;
  }

  // Prepare the next published hour in the background. No need to wait for a
  // complete model run; the run index lists hours individually as they arrive.
  async function prefetchNextHour(cycle, hour, domain, products) {
    const entries = entriesForCycle(cycle);
    const i = entries.findIndex(entry => entry.forecast_hour === hour);
    if (entries.length < 2 || i === -1) return;
    const next = entries[(i + 1) % entries.length];
    let manifest = manifestCache.get(next.manifest);
    if (!manifest) {
      try {
        const response = await fetch(dataUrl(next.manifest));
        if (!response.ok) return;
        const candidate = await response.json();
        if (candidate.model !== 'rrfs_mpas' || candidate.cycle !== cycle ||
            candidate.forecast_hour !== next.forecast_hour || !candidate.domains) return;
        manifestCache.set(next.manifest, candidate);
        manifest = candidate;
      } catch (_) { return; }
    }
    const images = manifest.domains?.[domain] || {};
    for (const product of products) {
      const relative = images[product];
      if (typeof relative !== 'string' ||
          !/^\d{10}\/(lbf|regional|central_plains)\/[\w.-]+\.png$/.test(relative) ||
          !relative.startsWith(cycle + '/' + domain + '/')) continue;
      preloadImage(dataUrl(relative) + '?v=' + state.cacheToken).catch(() => {});
    }
  }

  const isCompare = () => state.view !== 'single';
  const isFour = () => state.view === 'four_panel';
  const activeSlots = () => isFour() ? ['product', 'rightProduct', 'thirdProduct', 'fourthProduct']
                            : isCompare() ? ['product', 'rightProduct'] : ['product'];
  const auxPanels = {
    rightProduct: {image:'rightImage', loaded:'rightLoaded', url:'rightImageUrl',
      empty:'rightEmpty', emptyTitle:'rightEmptyTitle', emptyDetails:'rightEmptyDetails',
      title:'rightTitle', link:'rightFullLink'},
    thirdProduct: {image:'thirdImage', loaded:'thirdLoaded', url:'thirdImageUrl',
      empty:'thirdEmpty', emptyTitle:'thirdEmptyTitle', emptyDetails:'thirdEmptyDetails',
      title:'thirdTitle', link:'thirdFullLink'},
    fourthProduct: {image:'fourthImage', loaded:'fourthLoaded', url:'fourthImageUrl',
      empty:'fourthEmpty', emptyTitle:'fourthEmptyTitle', emptyDetails:'fourthEmptyDetails',
      title:'fourthTitle', link:'fourthFullLink'}
  };
  // Keep all four maps in the visible viewport rather than letting the second
  // row extend below the fold. The image files remain at original resolution.
  let fourFitPending = false;
  function scheduleFourPanelFit() {
    if (fourFitPending) return;
    fourFitPending = true;
    requestAnimationFrame(() => {
      fourFitPending = false;
      const wrap = byId('plotWrap');
      if (!isFour()) {
        wrap.style.removeProperty('--four-fit-height');
        return;
      }
      const top = Math.max(0, wrap.getBoundingClientRect().top);
      const available = Math.max(180, Math.floor(window.innerHeight - top - 7));
      wrap.style.setProperty('--four-fit-height', available + 'px');
    });
  }
  function setCompareView() {
    document.body.classList.toggle('four-panel-mode', isFour());
    focusButton.hidden = !isFour();
    if (!isFour() && focusActive) setFocusView(false);
    els.view.value = state.view;
    els.rightBlock.hidden = !isCompare();
    els.rightPanel.hidden = !isCompare();
    els.thirdBlock.hidden = !isFour();
    els.fourthBlock.hidden = !isFour();
    els.thirdPanel.hidden = !isFour();
    els.fourthPanel.hidden = !isFour();
    const wrap = byId('plotWrap');
    wrap.classList.toggle('compare', state.view === 'compare_products');
    wrap.classList.toggle('four', isFour());
    els.save.textContent = isFour() ? 'Save Four-Panel PNG' :
                           isCompare() ? 'Save Two-Panel PNG' : 'Save PNG';
    if (isFour() && window.scrollY > 0) window.scrollTo(0,0);
    scheduleFourPanelFit();
  }
  function updateSaveButton() {
    els.save.disabled = !activeSlots().every(slot => slot === 'product' ? state.leftLoaded :
       Boolean(state[auxPanels[slot].loaded]));
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
  function emptyExtra(slot, title, detail) {
    const p = auxPanels[slot];
    state[p.loaded] = false;
    state[p.url] = null;
    els[p.image].hidden = true;
    els[p.image].removeAttribute('src');
    els[p.empty].hidden = false;
    els[p.emptyTitle].textContent = title;
    els[p.emptyDetails].textContent = detail;
    els[p.link].hidden = true;
    els[p.link].removeAttribute('href');
    updateSaveButton();
  }
  function hideUnavailablePanel(slot, hour) {
    const label = productLabel(state[slot]);
    if (slot === 'product') {
      empty(label + ' unavailable at ' + fmtHour(hour), 'This forecast hour has no published PNG for the selected product. The product selection is preserved.');
    } else {
      emptyExtra(slot, label + ' unavailable at ' + fmtHour(hour),
        'No PNG for this product at the selected forecast hour.');
      els[auxPanels[slot].title].textContent = label;
    }
  }
  function catalogForCurrentDomain() {
    const id = `${state.cycle}:${state.domain}`;
    if (!state.productCatalog.has(id)) state.productCatalog.set(id, new Set());
    return state.productCatalog.get(id);
  }
  function sortedKeys(keys) {
    return [...keys].sort((a,b) => {
      const ai=PRODUCT_ORDER.indexOf(a), bi=PRODUCT_ORDER.indexOf(b);
      return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi) || a.localeCompare(b);
    });
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
    state.playing = false;
    state.playGeneration += 1;
    if (state.timer !== null) window.clearTimeout(state.timer);
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
    for (const [param, key] of [['thirdProduct','thirdProduct'], ['fourthProduct','fourthProduct']]) {
      if (isFour()) url.searchParams.set(param, state[key]);
      else url.searchParams.delete(param);
    }
    history.replaceState(null, '', url);
  }
  function renderHourControls() {
    const entries = entriesForCycle(state.cycle);
    const hours = entries.map(e => e.forecast_hour);
    const max = hours.length ? Math.max(...hours) : 0;
    els.slider.disabled = hours.length < 2;
    // F000 is initialization, not a clickable/animated forecast hour.
    els.slider.min = String(hours.length ? Math.min(...hours) : 1);
    els.slider.max = String(max);
    els.slider.step = '1';
    els.slider.value = String(state.hour ?? 0);
    els.fhr.textContent = Number.isInteger(state.hour) ? fmtTile(state.hour) : '---';
    els.play.disabled = hours.length < 2;
    els.latest.disabled = !hours.length;
    const signature = state.cycle + '|' + hours.join(',');
    if (els.tiles.dataset.signature !== signature) {
      els.tiles.replaceChildren();
      els.tiles.dataset.signature = signature;
      const available = new Set(hours);
      const fragment = document.createDocumentFragment();
      for (let hour = 0; hour <= max; hour++) {
        const exists = hour > 0 && available.has(hour);
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = fmtTile(hour);
        btn.className = 'frame ' + (exists ? 'available' : 'missing');
        btn.disabled = !exists;
        btn.dataset.hour = String(hour);
        btn.setAttribute('aria-label', fmtHour(hour) + (hour === 0 ? ' initialization hour disabled' : exists ? ' available' : ' not published'));
        if (exists) btn.addEventListener('click', () => {
          stopAnimation();
          void chooseHour(hour);
        });
        fragment.appendChild(btn);
      }
      els.tiles.appendChild(fragment);
      scheduleFourPanelFit();
    }
    // Update the active border only; don't rebuild/flash 49 tile elements.
    for (const btn of els.tiles.children) {
      const active = Number(btn.dataset.hour) === state.hour;
      btn.classList.toggle('active', active);
      if (active) btn.setAttribute('aria-current', 'true');
      else btn.removeAttribute('aria-current');
    }
  }
  function resetForNoRuns(message, level = 'warning') {
    ++state.serial;
    state.ready = false;
    state.entries = [];
    state.productCatalog.clear();
    state.cycle = null;
    state.hour = null;
    state.manifest = null;
    setSelectOptions(els.run, [], null);
    setSelectOptions(els.product, [], null);
    setSelectOptions(els.rightProduct, [], null);
    setSelectOptions(els.thirdProduct, [], null);
    setSelectOptions(els.fourthProduct, [], null);
    state.initializedProducts = false;
    emptyRight('Awaiting second product', 'Publish winter products to enable comparison views.');
    emptyExtra('thirdProduct', 'Awaiting third product', 'Publish winter products to enable four panels.');
    emptyExtra('fourthProduct', 'Awaiting fourth product', 'Publish winter products to enable four panels.');
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
        if (!Number.isInteger(item.forecast_hour) || item.forecast_hour <= 0 || item.forecast_hour > 120) continue; // F000 intentionally disabled
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
      if (!preserveSelection || oldCycle !== state.cycle) state.initializedProducts = false;
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
    if (!selected) return false;
    const serial = ++state.serial;
    const key = selected.manifest;
    renderHourControls();
    els.fhr.textContent = fmtTile(state.hour);
    els.validTime.textContent = utcTime(selected.valid_time_utc);
    status('Loading ' + state.cycle + 'Z ' + fmtHour(state.hour) + '…');
    // IMPORTANT: do not clear/hide the current PNG while the next hour loads.
    if (els.image.hidden) {
      els.empty.hidden = false;
      els.emptyTitle.textContent = 'Loading winter forecast…';
      els.emptyDetails.textContent = 'Retrieving the available forecast map for this hour.';
    }
    try {
      let manifest = manifestCache.get(key);
      if (!manifest) {
        const response = await fetch(dataUrl(key));
        if (!response.ok) throw new Error(`HTTP ${response.status} reading ${key}`);
        manifest = await response.json();
        if (manifest.model !== 'rrfs_mpas' || manifest.cycle !== selected.cycle ||
            manifest.forecast_hour !== selected.forecast_hour ||
            !manifest.domains || typeof manifest.domains !== 'object') {
          throw new Error('Manifest does not match the selected model hour');
        }
        manifestCache.set(key, manifest);
      }
      if (serial !== state.serial) return false;
      const loaded = await showManifest(manifest, serial);
      if (serial !== state.serial) return false;
      if (!loaded) return false;
      state.manifest = manifest;
      const allSelectedAvailable = activeSlots().every(slot => slot === 'product' ? state.leftLoaded : state[auxPanels[slot].loaded]);
      status(`Run ${state.cycle}Z · ${fmtHour(state.hour)} · ${utcTime(manifest.valid_time_utc)}` +
        (allSelectedAvailable ? '' : ' · Some selected products not available at this hour'),
        allSelectedAvailable ? 'success' : 'warning');
      syncAddress();
      return true;
    } catch (err) {
      if (serial !== state.serial) return false;
      // The prior PNG is intentionally left visible on a temporary S3 error.
      if (els.image.hidden) empty('Forecast map unavailable', 'Could not load the published forecast hour.');
      status('Map unavailable: ' + err.message, 'error');
      return false;
    }
  }

  async function showManifest(manifest, serial = state.serial) {
    const available = manifest.domains[state.domain] || {};
    const currentKeys = sortedKeys(Object.keys(available).filter(k => typeof available[k] === 'string'));
    const catalog = catalogForCurrentDomain();
    for (const key of currentKeys) catalog.add(key);
    // Keep previously discovered product choices in dropdowns even if a
    // subsequent hour has no QPF (or another product). Do not silently swap
    // products while the user steps through forecast hours.
    const keys = sortedKeys(catalog);
    if (!keys.length) {
      empty('No maps for this domain', `No ${DOMAIN_LABELS[state.domain]} maps are published for ${fmtHour(state.hour)}.`);
      els.title.textContent = 'RRFS-MPAS | No published maps';
      for (const slot of ['rightProduct','thirdProduct','fourthProduct'])
        emptyExtra(slot, 'Not available', 'No maps published for this domain and hour.');
      return false;
    }
    const preferred = {
      product: state.product,
      rightProduct: state.rightProduct,
      thirdProduct: state.thirdProduct,
      fourthProduct: state.fourthProduct
    };
    const defaultChoices = ['ptype_ref1km','qpf','t2m','fgen_850mb'];
    const used = new Set();
    const slots = ['product','rightProduct','thirdProduct','fourthProduct'];
    const selectors = [els.product, els.rightProduct, els.thirdProduct, els.fourthProduct];
    for (let i=0;i<slots.length;i++) {
      const slot=slots[i];
      let wanted=preferred[slot];
      if (!state.initializedProducts) {
        const ideal=defaultChoices[i];
        wanted = keys.includes(wanted) && !used.has(wanted) ? wanted :
                 keys.includes(ideal) && !used.has(ideal) ? ideal :
                 keys.find(k => !used.has(k)) || keys[0];
      }
      // Keep an explicitly selected product even when missing at this hour.
      setSelectOptions(selectors[i], keys.map(k => [k,productLabel(k)]), wanted);
      state[slot] = selectors[i].value;
      used.add(state[slot]);
    }
    state.initializedProducts = true;
    const validPath = path => typeof path === 'string' &&
      /^\d{10}\/(lbf|regional|central_plains)\/[\w.-]+\.png$/.test(path) &&
      path.startsWith(state.cycle + '/' + state.domain + '/');
    const active = activeSlots();
    const requests = active.map(slot => {
      const path = available[state[slot]];
      return {slot, url: validPath(path) ? dataUrl(path) + '?v=' + state.cacheToken : null};
    });
    const cycle = state.cycle, hour = state.hour, domain = state.domain;
    for (const slot of ['product','rightProduct','thirdProduct','fourthProduct']) {
      if (slot === 'product') state.leftLoaded = false;
      else state[auxPanels[slot].loaded] = false;
    }
    updateSaveButton();
    try {
      // Wait for all available PNGs in the chosen layout before swapping any
      // panel. On slow S3 connections, existing maps remain visible while loading.
      await Promise.all(requests.filter(r => r.url).map(r => preloadImage(r.url)));
      if (serial !== state.serial) return false;
      for (const {slot,url} of requests) {
        if (!url) {
          hideUnavailablePanel(slot, hour);
          continue;
        }
        const img = slot === 'product' ? els.image : els[auxPanels[slot].image];
        img.src=url;
        img.hidden=false;
        img.alt=`${productLabel(state[slot])} for ${DOMAIN_LABELS[domain]}, valid ${utcTime(manifest.valid_time_utc)}`;
        if (slot === 'product') {
          state.imageUrl=url;
          state.leftLoaded=true;
          els.empty.hidden=true;
          els.title.textContent=isCompare() ? productLabel(state.product) : `RRFS-MPAS | ${productLabel(state.product)}`;
          els.meta.textContent=`${DOMAIN_LABELS[domain].toUpperCase()} · ${fmtHour(hour)}`;
          els.caption.textContent=`${cycle}Z run · Valid ${utcTime(manifest.valid_time_utc)} · ${DOMAIN_LABELS[domain]}`;
          els.fullLink.href=url;
          els.fullLink.hidden=false;
        } else {
          const p = auxPanels[slot];
          state[p.url]=url;
          state[p.loaded]=true;
          els[p.empty].hidden=true;
          els[p.title].textContent=productLabel(state[slot]);
          els[p.link].href=url;
          els[p.link].hidden=false;
        }
      }
      els.viewerTitle.textContent = isFour() ? 'RRFS-MPAS Four-Panel Comparison' :
        isCompare() ? 'RRFS-MPAS Product Comparison' : `RRFS-MPAS ${productLabel(state.product)}`;
      updateSaveButton();
      void prefetchNextHour(cycle, hour, domain, active.map(slot => state[slot]));
      // Hour navigation may advance even when a single optional product is
      // missing, while showing a truthful unavailable panel for that product.
      return true;
    } catch (err) {
      if (serial !== state.serial) return false;
      status('S3 image could not be loaded: ' + err.message, 'error');
      return false;
    }
  }

  function chooseHour(hour) {
    if (!state.entries.some(e => e.cycle === state.cycle && e.forecast_hour === hour)) return Promise.resolve(false);
    state.hour = hour;
    return loadCurrent();
  }

  function startAnimation() {
    const entries = entriesForCycle(state.cycle);
    if (entries.length < 2) return;
    const hours = entries.map(e => e.forecast_hour);
    stopAnimation();
    state.playing = true;
    const generation = state.playGeneration;
    els.play.textContent = 'Ⅱ Pause';
    els.play.setAttribute('aria-pressed', 'true');
    const advance = async () => {
      if (!state.playing || generation !== state.playGeneration) return;
      state.timer = null;
      const idx = hours.indexOf(state.hour);
      const nextHour = hours[(idx + 1 + hours.length) % hours.length];
      // Never outrun S3: wait for the next PNG before scheduling another tick.
      const loaded = await chooseHour(nextHour);
      if (!state.playing || generation !== state.playGeneration) return;
      if (!loaded) {
        stopAnimation();
        status(`Playback paused: ${fmtHour(nextHour)} could not be loaded.`, 'warning');
        return;
      }
      state.timer = window.setTimeout(advance, FRAME_DELAY_MS);
    };
    state.timer = window.setTimeout(advance, FRAME_DELAY_MS);
  }

  setCompareView();
  els.view.addEventListener('change', () => {
    stopAnimation();
    state.view = els.view.value;
    setCompareView();
    ++state.serial;
    if (state.manifest) void showManifest(state.manifest, state.serial);
    syncAddress();
  });
  for (const slot of ['rightProduct','thirdProduct','fourthProduct']) {
    const select = slot === 'rightProduct' ? els.rightProduct :
                   slot === 'thirdProduct' ? els.thirdProduct : els.fourthProduct;
    select.addEventListener('change', () => {
      stopAnimation();
      state[slot] = select.value;
      ++state.serial;
      if (state.manifest) void showManifest(state.manifest, state.serial);
      syncAddress();
    });
  }
  els.domain.value = state.domain;
  els.domain.addEventListener('change', () => {
    stopAnimation();
    state.domain = els.domain.value;
    state.initializedProducts=false;
    ++state.serial;
    if (state.manifest) { void showManifest(state.manifest, state.serial); syncAddress(); }
  });
  els.product.addEventListener('change', () => {
    stopAnimation();
    state.product = els.product.value;
    ++state.serial;
    if (state.manifest) { void showManifest(state.manifest, state.serial); syncAddress(); }
  });
  els.run.addEventListener('change', () => {
    stopAnimation();
    state.cycle = els.run.value;
    const entries = entriesForCycle(state.cycle);
    state.hour = entries.length ? entries[0].forecast_hour : null;
    state.manifest = null;
    state.initializedProducts=false;
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
  els.play.addEventListener('click', () => state.playing ? stopAnimation() : startAnimation());
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
    if (els.save.disabled || !state.cycle || state.hour === null) return;
    const slots=activeSlots();
    const urls=slots.map(slot => slot === 'product' ? state.imageUrl : state[auxPanels[slot].url]);
    if (!urls.every(Boolean)) return;
    els.save.disabled=true;
    const mode = isFour() ? 'four_panel' : isCompare() ? 'two_panel' : 'single';
    const filename=`rrfs_mpas_winter_${mode}_${slots.map(s => state[s]).join('_vs_')}_${state.domain}_${state.cycle}_f${String(state.hour).padStart(3,'0')}.png`;
    try {
      const blobs=await Promise.all(urls.map(async url => {
        const response=await fetch(url);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const blob=await response.blob();
        if (!blob.type.startsWith('image/') && blob.type !== 'application/octet-stream')
          throw new Error('Unexpected file type');
        return blob;
      }));
      let output=blobs[0];
      if (slots.length>1) {
        const images=await Promise.all(blobs.map(blob => createImageBitmap(blob)));
        try {
          const width=Math.max(...images.map(img => img.width));
          const height=Math.max(...images.map(img => img.height));
          const columns=2;
          const rows=slots.length===4 ? 2 : 1;
          const canvas=document.createElement('canvas');
          canvas.width=width*columns;
          canvas.height=height*rows;
          const ctx=canvas.getContext('2d');
          if (!ctx) throw new Error('Canvas is unavailable');
          ctx.fillStyle='#000';
          ctx.fillRect(0,0,canvas.width,canvas.height);
          images.forEach((img,i) => {
            const scale=Math.min(width/img.width,height/img.height);
            const w=img.width*scale,h=img.height*scale;
            const x=(i%columns)*width+(width-w)/2;
            const y=Math.floor(i/columns)*height+(height-h)/2;
            ctx.drawImage(img,x,y,w,h);
          });
          output=await new Promise((resolve,reject) => canvas.toBlob(
            b=>b ? resolve(b) : reject(new Error('Canvas export failed')),'image/png'));
        } finally { images.forEach(img => img.close()); }
      }
      const blobUrl=URL.createObjectURL(output);
      const a=document.createElement('a');
      a.href=blobUrl;
      a.download=filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.setTimeout(() => URL.revokeObjectURL(blobUrl),2000);
      status(`${isFour() ? 'Four-panel' : isCompare() ? 'Two-panel' : 'Single-panel'} PNG download started.`,'success');
    } catch (err) {
      status('PNG saving needs AWS S3 CORS access. Open the original PNG links to save images separately.','warning');
    } finally {
      updateSaveButton();
    }
  });
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && focusActive) {
      event.preventDefault();
      setFocusView(false);
      return;
    }
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
  window.addEventListener('resize', scheduleFourPanelFit, {passive:true});
  window.addEventListener('orientationchange', scheduleFourPanelFit);
  if (typeof ResizeObserver !== 'undefined') {
    const fitObserver = new ResizeObserver(scheduleFourPanelFit);
    for (const target of ['.site-header','.topbar','.controls','.save-controls','.tiles']) {
      const element = document.querySelector(target);
      if (element) fitObserver.observe(element);
    }
  }
  refreshRuns(false);
})();
