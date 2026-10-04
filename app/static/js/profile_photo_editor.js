/* Photo edits stay local until Save; the personal-details form is never submitted. */
(() => {
  const form = document.querySelector('[data-photo-editor-form]');
  const dialog = document.getElementById('profile-photo-editor');
  if (!form || !dialog || typeof dialog.showModal !== 'function') return;

  const fileInput = form.querySelector('input[name="photo"]');
  const versionInput = form.querySelector('input[name="photo_version"]');
  const editButton = form.querySelector('[data-photo-edit]');
  const feedback = form.querySelector('[data-photo-feedback]');
  const surface = dialog.querySelector('[data-photo-surface]');
  const canvas = dialog.querySelector('[data-photo-canvas]');
  const avatarCanvas = dialog.querySelector('[data-photo-avatar]');
  const context = canvas.getContext('2d');
  const avatarContext = avatarCanvas.getContext('2d');
  if (!context || !avatarContext) return;

  const workspace = dialog.querySelector('[data-photo-workspace]');
  const zoomInput = dialog.querySelector('[data-photo-zoom]');
  const zoomValue = dialog.querySelector('[data-photo-zoom-value]');
  const resetButton = dialog.querySelector('[data-photo-reset]');
  const panButtons = [...dialog.querySelectorAll('[data-photo-pan]')];
  const saveButton = dialog.querySelector('[data-photo-save]');
  const cancelButton = dialog.querySelector('[data-photo-cancel]');
  const closeButton = dialog.querySelector('[data-photo-close]');
  const error = dialog.querySelector('[data-photo-error]');
  const status = dialog.querySelector('[data-photo-status]');
  const labels = form.dataset;
  let current = null;
  let loadController = null;
  let generation = 0;
  let saving = false;
  let pointer = null;
  let previousFocus = null;

  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const userError = message => Object.assign(new Error(message), { userMessage: true });
  const clearError = () => { error.textContent = ''; error.hidden = true; };
  const showError = message => { error.textContent = message; error.hidden = false; };
  const showFeedback = (message, isError = false) => {
    feedback.textContent = message;
    feedback.classList.toggle('is-error', isError);
    feedback.hidden = !message;
  };

  const setControls = () => {
    const ready = Boolean(current?.image) && !saving;
    [zoomInput, resetButton, saveButton, ...panButtons].forEach(element => { element.disabled = !ready; });
    [closeButton, cancelButton].forEach(element => { element.disabled = saving; });
    fileInput.disabled = saving;
    editButton.disabled = saving;
    surface.setAttribute('aria-disabled', String(!ready));
    workspace.setAttribute('aria-busy', String(!current?.image || saving));
  };

  const crop = state => {
    state.zoom = clamp(state.zoom, 1, 8);
    const side = Math.min(state.width, state.height) / state.zoom;
    const halfX = side / (2 * state.width);
    const halfY = side / (2 * state.height);
    state.centerX = clamp(state.centerX, halfX, 1 - halfX);
    state.centerY = clamp(state.centerY, halfY, 1 - halfY);
    return { x: state.centerX * state.width - side / 2, y: state.centerY * state.height - side / 2, side };
  };

  const render = () => {
    if (!current?.image) return;
    const region = crop(current);
    for (const [target, drawing] of [[canvas, context], [avatarCanvas, avatarContext]]) {
      drawing.fillStyle = '#fff';
      drawing.fillRect(0, 0, target.width, target.height);
      drawing.drawImage(current.image, region.x, region.y, region.side, region.side, 0, 0, target.width, target.height);
    }
    zoomInput.value = String(current.zoom);
    const percentage = `${Math.round(current.zoom * 100)}%`;
    zoomValue.value = percentage;
    zoomInput.setAttribute('aria-valuetext', percentage);
  };

  const releasePointer = () => {
    if (pointer && surface.hasPointerCapture(pointer.id)) surface.releasePointerCapture(pointer.id);
    pointer = null;
    surface.classList.remove('is-dragging');
  };

  const releaseCurrent = () => {
    generation += 1;
    loadController?.abort();
    loadController = null;
    releasePointer();
    if (current?.objectUrl) URL.revokeObjectURL(current.objectUrl);
    current = null;
  };

  const closeEditor = () => {
    if (!saving) dialog.close();
  };

  dialog.addEventListener('close', () => {
    // A previous close event can arrive after a newer file has reopened the dialog.
    if (dialog.open) return;
    releaseCurrent();
    fileInput.value = '';
    document.body.classList.remove('photo-editor-open');
    clearError();
    status.textContent = '';
    setControls();
    if (previousFocus?.isConnected && !previousFocus.disabled) previousFocus.focus({ preventScroll: true });
    previousFocus = null;
  });
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    closeEditor();
  });
  // Keep Escape local to the native dialog rather than the page's navigation.
  dialog.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeEditor();
    }
  });
  cancelButton.addEventListener('click', closeEditor);
  closeButton.addEventListener('click', closeEditor);

  const readJSON = async (response, fallback) => {
    let result;
    try { result = await response.json(); } catch (_error) { throw userError(fallback); }
    if (!response.ok) throw userError(result?.error || fallback);
    return result;
  };

  const loadImage = url => new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(userError(labels.loadError));
    image.src = url;
  });

  const openEditor = async (file = null) => {
    if (saving) return;
    releaseCurrent();
    const token = generation;
    const state = { file, image: null, objectUrl: null, version: versionInput.value, zoom: 1, centerX: .5, centerY: .5 };
    current = state;
    loadController = new AbortController();
    previousFocus = file ? fileInput : editButton;
    showFeedback('');
    clearError();
    status.textContent = labels.loadingLabel;
    context.clearRect(0, 0, canvas.width, canvas.height);
    avatarContext.clearRect(0, 0, avatarCanvas.width, avatarCanvas.height);
    zoomInput.value = '1';
    zoomValue.value = '100%';
    setControls();
    if (!dialog.open) dialog.showModal();
    document.body.classList.add('photo-editor-open');

    try {
      let imageUrl;
      if (file) {
        if (!/\.(jpe?g|png|webp)$/i.test(file.name)) throw userError(labels.typeError);
        if (file.size > Number(labels.maxBytes)) throw userError(labels.sizeError);
        state.objectUrl = URL.createObjectURL(file);
        imageUrl = state.objectUrl;
      } else {
        const response = await fetch(labels.editorUrl, { headers: { Accept: 'application/json' }, cache: 'no-store', signal: loadController.signal });
        const metadata = await readJSON(response, labels.loadError);
        if (token !== generation || current !== state) return;
        if (!metadata.image_url || !metadata.version || !Number.isFinite(metadata.center_x) || !Number.isFinite(metadata.center_y) || !Number.isFinite(metadata.zoom)) throw userError(labels.loadError);
        state.version = metadata.version;
        state.centerX = metadata.center_x;
        state.centerY = metadata.center_y;
        state.zoom = metadata.zoom;
        imageUrl = metadata.image_url;
      }
      const image = await loadImage(imageUrl);
      if (token !== generation || current !== state || !dialog.open) return;
      if (!image.naturalWidth || !image.naturalHeight) throw userError(labels.loadError);
      if (image.naturalWidth * image.naturalHeight > 20_000_000 || Math.max(image.naturalWidth, image.naturalHeight) > 10_000) throw userError(labels.pixelsError);
      state.image = image;
      state.width = image.naturalWidth;
      state.height = image.naturalHeight;
      // Browser image decoding and the server both honour the source EXIF orientation.
      if (!file) versionInput.value = state.version;
      status.textContent = '';
      render();
      setControls();
      if (document.activeElement === closeButton || document.activeElement === dialog) surface.focus({ preventScroll: true });
    } catch (failure) {
      if (token !== generation || current !== state || failure.name === 'AbortError') return;
      status.textContent = '';
      showError(failure.userMessage ? failure.message : labels.loadError);
      setControls();
    }
  };

  const pan = (deltaX, deltaY) => {
    if (!current?.image || saving) return;
    const size = surface.getBoundingClientRect().width;
    if (!size) return;
    const side = Math.min(current.width, current.height) / current.zoom;
    current.centerX -= deltaX / size * side / current.width;
    current.centerY -= deltaY / size * side / current.height;
    render();
  };

  surface.addEventListener('pointerdown', event => {
    if (!current?.image || saving || !event.isPrimary || event.button !== 0) return;
    event.preventDefault();
    surface.focus({ preventScroll: true });
    pointer = { id: event.pointerId, x: event.clientX, y: event.clientY };
    surface.setPointerCapture(event.pointerId);
    surface.classList.add('is-dragging');
  });
  surface.addEventListener('pointermove', event => {
    if (!pointer || pointer.id !== event.pointerId) return;
    pan(event.clientX - pointer.x, event.clientY - pointer.y);
    pointer.x = event.clientX;
    pointer.y = event.clientY;
  });
  ['pointerup', 'pointercancel', 'lostpointercapture'].forEach(type => surface.addEventListener(type, releasePointer));

  const directions = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  surface.addEventListener('keydown', event => {
    const direction = directions[event.key];
    if (!direction || !current?.image || saving) return;
    event.preventDefault();
    const step = event.shiftKey ? 30 : 8;
    pan(direction[0] * step, direction[1] * step);
  });
  panButtons.forEach(button => button.addEventListener('click', () => {
    const direction = directions[`Arrow${button.dataset.photoPan[0].toUpperCase()}${button.dataset.photoPan.slice(1)}`];
    pan(direction[0] * 18, direction[1] * 18);
  }));
  zoomInput.addEventListener('input', () => {
    if (!current?.image || saving) return;
    current.zoom = Number(zoomInput.value);
    render();
  });
  surface.addEventListener('wheel', event => {
    if (!current?.image || saving || event.ctrlKey) return;
    event.preventDefault();
    const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 300 : 1);
    current.zoom *= Math.exp(-clamp(delta, -200, 200) * .002);
    render();
  }, { passive: false });
  resetButton.addEventListener('click', () => {
    if (!current?.image || saving) return;
    current.centerX = .5;
    current.centerY = .5;
    current.zoom = 1;
    render();
  });

  saveButton.addEventListener('click', async () => {
    if (!current?.image || saving) return;
    const state = current;
    const token = generation;
    crop(state);
    const data = new FormData();
    data.set('csrf_token', form.querySelector('input[name="csrf_token"]').value);
    data.set('photo_version', state.version);
    data.set('crop_center_x', String(state.centerX));
    data.set('crop_center_y', String(state.centerY));
    data.set('crop_zoom', String(state.zoom));
    if (state.file) data.set('photo', state.file, state.file.name);
    else data.set('use_existing', '1');
    saving = true;
    releasePointer();
    clearError();
    status.textContent = labels.savingLabel;
    setControls();
    try {
      const response = await fetch(form.action, { method: 'POST', body: data, headers: { Accept: 'application/json' } });
      const result = await readJSON(response, labels.saveError);
      if (token !== generation || current !== state) return;
      if (!result.photo_url || !result.version) throw userError(labels.saveError);
      versionInput.value = result.version;
      document.querySelectorAll('.profile-photo-preview, .sidebar-account .account-avatar').forEach(avatar => {
        const image = new Image();
        image.src = result.photo_url;
        image.alt = '';
        image.width = avatar.classList.contains('profile-photo-preview') ? 80 : 42;
        image.height = image.width;
        avatar.replaceChildren(image);
      });
      editButton.hidden = false;
      showFeedback(labels.savedLabel);
      saving = false;
      dialog.close();
    } catch (failure) {
      if (token !== generation || current !== state) return;
      status.textContent = '';
      showError(failure.userMessage ? failure.message : labels.saveError);
    } finally {
      saving = false;
      setControls();
    }
  });

  fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    if (file) openEditor(file);
  });
  editButton.addEventListener('click', () => openEditor());
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (!saving && fileInput.files[0]) openEditor(fileInput.files[0]);
  });
  form.querySelector('[data-photo-plain-upload]').hidden = true;
  editButton.hidden = !versionInput.value;
})();
