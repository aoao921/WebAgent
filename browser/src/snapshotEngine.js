export const SNAPSHOT_SCRIPT = String.raw`(() => {
  const ROOT = document.body;
  if (!ROOT) {
    return { tree: [], ref_map: {}, yaml: '' };
  }

  const text = (v) => (v || '')
    .replace(/[\u200b\u00ad]/g, '')
    .replace(/\s+/g, ' ')
    .trim();

  const MAX_NAME_LEN = 96;
  const MAX_TEXT_LEN = 120;
  const MAX_TEXT_ITEMS = 16;
  const MAX_DATA_ROWS = 4;
  const MAX_DATA_CELLS = 6;

  const shortText = (value, max = MAX_TEXT_LEN) => {
    const t = text(value);
    if (!t) return '';
    return t.length > max ? t.slice(0, max - 1) + '…' : t;
  };

  const implicitRole = (el) => {
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'footer') return 'contentinfo';
    if (tag === 'form') return 'form';
    if (tag === 'main') return 'main';
    if (tag === 'nav') return 'navigation';
    if (tag === 'ol' || tag === 'ul') return 'list';
    if (tag === 'li') return 'listitem';
    if (tag === 'p') return 'paragraph';
    if (tag === 'section') return el.hasAttribute('aria-label') || el.hasAttribute('aria-labelledby') ? 'region' : null;
    if (tag === 'article') return 'article';
    if (tag === 'aside') return 'complementary';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'img') return 'img';
    if (tag === 'h1' || tag === 'h2' || tag === 'h3' || tag === 'h4' || tag === 'h5' || tag === 'h6') return 'heading';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'button' || t === 'submit' || t === 'reset') return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'range') return 'slider';
      return 'textbox';
    }
    return null;
  };

  const nativeLabelText = (el) => {
    const labels = el.labels ? Array.from(el.labels) : [];
    if (!labels.length && el.id) {
      for (const label of document.querySelectorAll('label')) {
        if (label.htmlFor === el.id) labels.push(label);
      }
    }
    return text(labels.map((label) => label.textContent).join(' '));
  };

  const frameworkFormLabelText = (el) => {
    const formItem = el.closest?.(
      '.el-form-item,.ant-form-item,.ivu-form-item,.form-item,[data-form-item]'
    );
    if (!formItem) return '';

    const labelNode = formItem.querySelector(
      '.el-form-item__label,.ant-form-item-label,label,.ivu-form-item-label,.form-label'
    );
    if (!labelNode) return '';
    return text(labelNode.innerText || labelNode.textContent).replace(/[：:]\s*$/, '');
  };

  const frameworkValidationText = (el) => {
    const formItem = el.closest?.(
      '.el-form-item,.ant-form-item,.ivu-form-item,.form-item,[data-form-item]'
    );
    if (!formItem) return '';

    const errorNodes = Array.from(formItem.querySelectorAll(
      '.el-form-item__error,.ant-form-item-explain-error,.ivu-form-item-error-tip,' +
      '.invalid-feedback,.form-error,.error-message,[role="alert"],[data-validation-error]'
    )).filter((node) => isVisible(node));

    const messages = errorNodes
      .map((node) => text(node.innerText || node.textContent))
      .filter(Boolean);

    return Array.from(new Set(messages)).join('; ');
  };

  const isTextNameRole = (role) => {
    return ['button', 'link', 'heading', 'tab', 'menuitem', 'checkbox', 'radio', 'combobox'].includes(role);
  };

  const accName = (el, role, interactive) => {
    const label = text(el.getAttribute('aria-label'));
    if (label) return label;

    const labelledBy = text(el.getAttribute('aria-labelledby'));
    if (labelledBy) {
      const joined = labelledBy
        .split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((n) => text(n.textContent))
        .join(' ')
        .trim();
      if (joined) return joined;
    }

    if ((el.tagName || '').toLowerCase() === 'img') {
      const alt = text(el.getAttribute('alt'));
      if (alt) return alt;
    }

    const nativeLabel = nativeLabelText(el);
    if (nativeLabel) return nativeLabel;

    const frameworkLabel = ['textbox', 'combobox', 'spinbutton'].includes(role)
      ? frameworkFormLabelText(el)
      : '';
    if (frameworkLabel) return frameworkLabel;

    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'button' || type === 'submit' || type === 'reset') {
        const value = text(el.getAttribute('value'));
        if (value) return value;
      }
    }

    if (role === 'textbox' || role === 'combobox') {
      const placeholder = text(el.getAttribute('placeholder'));
      if (placeholder) return placeholder;
    }

    const title = text(el.getAttribute('title'));
    if (title) return title;

    if (role === 'generic' && !interactive)
      return '';

    const own = text(el.innerText || el.textContent);
    if (!interactive && !isTextNameRole(role))
      return '';
    return shortText(own, MAX_NAME_LEN);
  };

  const isVisible = (el) => {
    if (!el || !(el instanceof Element)) return false;
    for (let node = el; node && node instanceof Element; node = node.parentElement) {
      if (node.hidden) return false;
      if (node.getAttribute('aria-hidden') === 'true') return false;
      const style = window.getComputedStyle(node);
      if (!style) continue;
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      if (style.opacity === '0') return false;
    }
    if (el !== document.body && el.getClientRects().length === 0) return false;
    return true;
  };

  const isElementHiddenForAria = (el) => {
    if (!el || !(el instanceof Element)) return true;
    for (let node = el; node && node instanceof Element; node = node.parentElement) {
      if (node.hidden) return true;
      if (node.getAttribute('aria-hidden') === 'true') return true;
      const style = window.getComputedStyle(node);
      if (!style) continue;
      if (style.display === 'none' || style.visibility === 'hidden') return true;
    }
    return false;
  };

  const receivesPointerEvents = (el) => {
    const style = window.getComputedStyle(el);
    if (!style) return true;
    return style.pointerEvents !== 'none';
  };

  const cursorStyle = (el) => {
    const style = window.getComputedStyle(el);
    return style ? style.cursor : '';
  };

  const isInteractive = (el) => {
    const tag = (el.tagName || '').toLowerCase();
    if (['a', 'button', 'input', 'select', 'textarea', 'summary'].includes(tag)) return true;
    const tabIndex = el.getAttribute('tabindex');
    if (tabIndex !== null && Number(tabIndex) >= 0) return true;
    const role = (el.getAttribute('role') || '').toLowerCase();
    return ['button', 'link', 'textbox', 'checkbox', 'radio', 'combobox', 'menuitem', 'tab'].includes(role);
  };

  const isActionableElement = (el, interactive, visible, pointer, cursor) => {
    if (!visible || !pointer) return false;
    if (interactive) return true;
    if (typeof el.onclick === 'function') return true;
    if (el.hasAttribute('onclick')) return true;
    return cursor === 'pointer';
  };

  const getAriaStateProps = (el, role) => {
    const props = {};
    const tag = (el.tagName || '').toLowerCase();
    const inputType = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : '';

    if (['input', 'select', 'textarea'].includes(tag)) {
      const parameterName = text(el.getAttribute('name'));
      if (parameterName) props.parameter_name = shortText(parameterName, 160);
      props.input_type = tag === 'input' ? inputType : tag;
      const form = el.form || el.closest?.('form');
      if (form) {
        const action = form.getAttribute('action') || window.location.href;
        try {
          props.form_action = new URL(action, window.location.href).href;
        } catch {
          props.form_action = shortText(action, 500);
        }
        props.form_method = (form.getAttribute('method') || 'GET').toUpperCase();
      }
      if (inputType === 'password' || /password|passwd|secret|token|otp|captcha/i.test(parameterName))
        props.sensitive = true;
    }

    const checked = el.getAttribute('aria-checked');
    if (checked === 'true') props.checked = true;
    if (checked === 'mixed') props.checked = 'mixed';
    if (checked === null && (inputType === 'checkbox' || inputType === 'radio') && el.checked)
      props.checked = true;

    const disabled = el.getAttribute('aria-disabled');
    if (disabled === 'true') props.disabled = true;
    if (disabled === null && typeof el.disabled === 'boolean' && el.disabled)
      props.disabled = true;

    const expanded = el.getAttribute('aria-expanded');
    if (expanded === 'true') props.expanded = true;
    if (expanded === null && tag === 'details' && el.open)
      props.expanded = true;

    const selected = el.getAttribute('aria-selected');
    if (selected === 'true') props.selected = true;
    if (selected === null && tag === 'option' && el.selected)
      props.selected = true;

    const pressed = el.getAttribute('aria-pressed');
    if (pressed === 'true') props.pressed = true;
    if (pressed === 'mixed') props.pressed = 'mixed';

    const level = el.getAttribute('aria-level');
    if (level && /^\d+$/.test(level)) props.level = Number(level);
    if (!props.level && role === 'heading' && /^h[1-6]$/.test(tag))
      props.level = Number(tag.slice(1));

    if (role === 'link') {
      const href = el.getAttribute('href');
      if (href) props.url = href;
    }

    if (role === 'textbox') {
      const placeholder = el.getAttribute('placeholder');
      if (placeholder) props.placeholder = placeholder;
    }

    if (['textbox', 'combobox', 'radio', 'checkbox', 'spinbutton'].includes(role)) {
      const validation = frameworkValidationText(el);
      if (validation) {
        props.invalid = true;
        props.validation = shortText(validation, 160);
      }
    }

    return props;
  };

  const modeToOptions = (mode) => {
    if (mode === 'ai') {
      return {
        visibility: 'ariaOrVisible',
        refs: 'interactable'
      };
    }
    return {
      visibility: 'aria',
      refs: 'none'
    };
  };

  const visibilityPass = (el, visibilityMode) => {
    const ariaVisible = !isElementHiddenForAria(el);
    const visible = isVisible(el);
    if (visibilityMode === 'aria') return ariaVisible;
    if (visibilityMode === 'ariaOrVisible') return ariaVisible && visible;
    if (visibilityMode === 'ariaAndVisible') return ariaVisible && visible;
    return ariaVisible;
  };

  const ensureRef = (el, role, name, refPrefix) => {
    const key = '__simpleAriaRef';
    const prev = el[key];
    if (prev && prev.role === role && prev.name === name && prev.refPrefix === refPrefix && prev.ref) {
      el.setAttribute('data-snapshot-ref', prev.ref);
      return prev.ref;
    }

    const next = ((window.__simpleAriaRefCounter || 0) + 1);
    window.__simpleAriaRefCounter = next;
    const ref = refPrefix + 'e' + next;
    el[key] = { role, name, refPrefix, ref };
    el.setAttribute('data-snapshot-ref', ref);
    return ref;
  };

  const rectOf = (el) => {
    const rect = el.getBoundingClientRect();
    return {
      x: Math.round(rect.x),
      y: Math.round(rect.y),
      width: Math.round(rect.width),
      height: Math.round(rect.height)
    };
  };

  const areaOf = (bounds) => Math.max(0, bounds.width) * Math.max(0, bounds.height);

  const boundsOverlapRatio = (a, b) => {
    const x1 = Math.max(a.x, b.x);
    const y1 = Math.max(a.y, b.y);
    const x2 = Math.min(a.x + a.width, b.x + b.width);
    const y2 = Math.min(a.y + a.height, b.y + b.height);
    const overlap = Math.max(0, x2 - x1) * Math.max(0, y2 - y1);
    const smaller = Math.min(areaOf(a), areaOf(b));
    return smaller > 0 ? overlap / smaller : 0;
  };

  const cssHint = (el) => text([
    el.id || '',
    el.className && typeof el.className === 'string' ? el.className : '',
    el.getAttribute('data-testid') || '',
    el.getAttribute('data-test') || ''
  ].join(' ')).toLowerCase();

  const regionType = (el) => {
    const tag = (el.tagName || '').toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    const hint = cssHint(el);

    if (role === 'banner' || tag === 'header') return 'header';
    if (role === 'navigation' || tag === 'nav' || tag === 'aside' || /\b(nav|menu|sidebar|side)\b/.test(hint)) return 'navigation';
    if (role === 'main' || tag === 'main' || /\b(main|content|container)\b/.test(hint)) return 'main';
    if (role === 'complementary' || /\b(aside)\b/.test(hint)) return 'navigation';
    if (role === 'contentinfo' || tag === 'footer') return 'footer';
    if (role === 'dialog' || role === 'alertdialog' || /\b(modal|dialog|drawer)\b/.test(hint)) return 'dialog';
    if (role === 'tablist' || /\b(tab|tabs)\b/.test(hint)) return 'tablist';
    if (tag === 'form' || role === 'form' || /\b(form|search|filter|query)\b/.test(hint)) return 'form';
    if (tag === 'table' || role === 'table' || role === 'grid' || /\b(table|grid|list)\b/.test(hint)) return 'data';
    if (tag === 'section' || tag === 'article' || role === 'region') return 'section';
    return '';
  };

  const dialogRegionScore = (el, bounds, viewportArea) => {
    const role = (el.getAttribute('role') || '').toLowerCase();
    const hint = cssHint(el);
    let score = 0;
    if (role === 'dialog' || role === 'alertdialog') score += 45;
    if (/\b(el-dialog|ant-modal|modal-dialog|modal-content|ivu-modal|drawer-content)\b/.test(hint)) score += 35;
    if (/\b(wrapper|overlay|backdrop|mask|v-modal|modal-backdrop)\b/.test(hint)) score -= 35;
    if (areaOf(bounds) >= viewportArea * 0.8) score -= 25;
    return score;
  };

  const regionLabel = (el, type) => {
    const role = (el.getAttribute('role') || '').toLowerCase();
    const name = accName(el, role || implicitRole(el) || 'region', false);
    if (name) return name.length > 80 ? name.slice(0, 80) : name;

    const heading = el.querySelector('h1,h2,h3,[role="heading"]');
    const headingText = heading ? text(heading.innerText || heading.textContent) : '';
    if (headingText) return headingText.length > 80 ? headingText.slice(0, 80) : headingText;

    const labels = {
      header: '顶部区域',
      navigation: '导航区域',
      sidebar: '侧边栏',
      main: '主内容区',
      footer: '底部区域',
      dialog: '弹窗区域',
      tablist: '标签页区域',
      form: '表单区域',
      data: '数据展示区',
      section: '内容分区',
      page: '页面主体'
    };
    return labels[type] || '页面区域';
  };

  const summarizeRegion = (node) => {
    const counts = {};
    const names = [];
    const walkNode = (current) => {
      if (!current || typeof current === 'string') return;
      counts[current.role] = (counts[current.role] || 0) + 1;
      if (current.ref && current.name && names.length < 6) names.push(current.name);
      for (const child of current.children || []) walkNode(child);
    };
    walkNode(node);

    const parts = [];
    for (const [role, count] of Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 4)) {
      parts.push(role + ' ' + count);
    }
    if (names.length) parts.push('关键元素: ' + names.join('、'));
    return parts.join('；');
  };

  const isDataContainer = (el) => {
    const tag = (el.tagName || '').toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    return tag === 'table' || role === 'table' || role === 'grid' || /\b(table|grid|data-table)\b/.test(cssHint(el));
  };

  const collectDataPreview = (el) => {
    const table = isDataContainer(el) ? el : el.querySelector('table,[role="table"],[role="grid"]');
    const source = table || el;
    const headerCells = Array.from(source.querySelectorAll('thead th,[role="columnheader"]'))
      .map((cell) => shortText(cell.innerText || cell.textContent, 48))
      .filter(Boolean)
      .slice(0, MAX_DATA_CELLS);

    const rows = Array.from(source.querySelectorAll('tbody tr,[role="row"]'))
      .filter((row) => isVisible(row));
    const sampleRows = rows.slice(0, MAX_DATA_ROWS).map((row) => {
      return Array.from(row.querySelectorAll('td,th,[role="cell"],[role="gridcell"]'))
        .map((cell) => shortText(cell.innerText || cell.textContent, 48))
        .filter(Boolean)
        .slice(0, MAX_DATA_CELLS);
    }).filter((row) => row.length);

    const controls = Array.from(el.querySelectorAll('input,select,textarea,button,a[href],[role="button"],[role="link"],[tabindex]'))
      .filter((control) => isVisible(control))
      .map((control) => {
        const role = (control.getAttribute('role') || implicitRole(control) || 'control').toLowerCase();
        const name = accName(control, role, true);
        return shortText(name || control.getAttribute('placeholder') || control.getAttribute('name') || role, 64);
      })
      .filter(Boolean)
      .slice(0, 8);

    return {
      rows_estimate: rows.length || null,
      columns: headerCells,
      sample_rows: sampleRows,
      controls
    };
  };

  const keepActionableNodes = (nodes) => {
    const out = [];
    for (const node of nodes || []) {
      if (!node || typeof node === 'string') continue;
      const children = keepActionableNodes(node.children || []);
      const keep = node.interactive || node.cursor === 'pointer' || children.length || ['button', 'link', 'textbox', 'checkbox', 'radio', 'combobox', 'tab'].includes(node.role);
      if (keep) out.push({ ...node, children });
    }
    return out;
  };

  const renderKey = (node) => {
    let key = node.role;
    if (node.name) key += ' ' + JSON.stringify(node.name);
    if (node.checked === true) key += ' [checked]';
    if (node.checked === 'mixed') key += ' [checked=mixed]';
    if (node.disabled) key += ' [disabled]';
    if (node.expanded) key += ' [expanded]';
    if (node.level) key += ' [level=' + node.level + ']';
    if (node.pressed === true) key += ' [pressed]';
    if (node.pressed === 'mixed') key += ' [pressed=mixed]';
    if (node.selected === true) key += ' [selected]';
    if (node.ref) key += ' [ref=' + node.ref + ']';
    if (node.active) key += ' [active]';
    if (node.cursor === 'pointer') key += ' [cursor=pointer]';
    return key;
  };

  const normalizeGenerics = (nodes) => {
    const normalizeNode = (node) => {
      const outChildren = [];
      for (const child of node.children || []) {
        if (typeof child === 'string') {
          outChildren.push(child);
        } else {
          const normalized = normalizeNode(child);
          if (Array.isArray(normalized)) outChildren.push(...normalized);
          else outChildren.push(normalized);
        }
      }
      node.children = outChildren;

      const removeSelf = node.role === 'generic' && !node.name && outChildren.length <= 1 && outChildren.every(c => typeof c !== 'string');
      if (removeSelf) return outChildren;
      return node;
    };

    const out = [];
    for (const node of nodes) {
      if (typeof node === 'string') out.push(node);
      else {
        const n = normalizeNode(node);
        if (Array.isArray(n)) out.push(...n);
        else out.push(n);
      }
    }
    return out;
  };

  const renderNodeYamlLines = (nodes, depthLimit, baseDepth = 0) => {
    const lines = [];
    const seenText = new Set();
    let textItems = 0;
    const ind = (d) => '  '.repeat(d);

    const visitText = (txt, d) => {
      if (textItems >= MAX_TEXT_ITEMS) return;
      const t = shortText(txt);
      if (!t) return;
      if (seenText.has(t)) return;
      seenText.add(t);
      textItems += 1;
      lines.push(ind(d) + '- text: ' + JSON.stringify(t));
    };

    const visitNode = (node, d) => {
      const hasDepthLimit = depthLimit !== null && depthLimit !== undefined;
      if (hasDepthLimit && d > depthLimit) return;
      const atLimit = hasDepthLimit && d === depthLimit;
      const key = ind(d) + '- ' + renderKey(node);

      const props = Object.entries(node.props || {})
        .filter(([k]) => !['checked', 'disabled', 'expanded', 'level', 'pressed', 'selected'].includes(k));
      const hasChildren = (node.children || []).length > 0 && !atLimit;

      if (!node.name && node.role === 'paragraph' && !props.length && !atLimit && node.children && node.children.length === 1 && typeof node.children[0] === 'string') {
        lines.push(key + ': ' + JSON.stringify(shortText(node.children[0])));
        return;
      }

      if (!props.length && !hasChildren) {
        lines.push(key);
        return;
      }

      lines.push(key + ':');
      for (const [k, v] of props) {
        lines.push(ind(d + 1) + '- /' + k + ': ' + JSON.stringify(String(v)));
      }

      if (!atLimit) {
        for (const child of node.children || []) {
          if (typeof child === 'string') visitText(child, d + 1);
          else visitNode(child, d + 1);
        }
      }
    };

    for (const node of nodes) {
      if (typeof node === 'string') visitText(node, baseDepth);
      else visitNode(node, baseDepth);
    }

    return lines;
  };

  const renderYaml = (nodes, depthLimit) => renderNodeYamlLines(nodes, depthLimit, 0).join('\n');

  const renderRegionsYaml = (regions, depthLimit) => {
    const lines = ['page_regions:'];
    if (!regions.length) {
      lines.push('  - region r1 "页面主体" [type=page]:');
      for (const line of renderNodeYamlLines(tree, depthLimit, 2)) lines.push(line);
      return lines.join('\n');
    }

    for (const region of regions) {
      let title = '  - region ' + region.id + ' ' + JSON.stringify(region.label) + ' [type=' + region.type + ']';
      if (region.bounds)
        title += ' [bounds=' + region.bounds.x + ',' + region.bounds.y + ',' + region.bounds.width + ',' + region.bounds.height + ']';
      lines.push(title + ':');
      if (region.summary) lines.push('    - summary: ' + JSON.stringify(region.summary));
      if (region.child_regions && region.child_regions.length) {
        lines.push('    - child_regions:');
        for (const childRegion of region.child_regions) {
          lines.push('      - ' + childRegion.id + ' ' + JSON.stringify(childRegion.label) + ' [type=' + childRegion.type + ']');
        }
      }
      if (region.data_preview) {
        lines.push('    - data_preview:');
        if (region.data_preview.rows_estimate !== null && region.data_preview.rows_estimate !== undefined)
          lines.push('      - rows_estimate: ' + region.data_preview.rows_estimate);
        if (region.data_preview.columns && region.data_preview.columns.length)
          lines.push('      - columns: ' + JSON.stringify(region.data_preview.columns));
        if (region.data_preview.sample_rows && region.data_preview.sample_rows.length) {
          lines.push('      - sample_rows:');
          for (const row of region.data_preview.sample_rows) {
            lines.push('        - ' + JSON.stringify(row));
          }
        }
        if (region.data_preview.controls && region.data_preview.controls.length)
          lines.push('      - controls: ' + JSON.stringify(region.data_preview.controls));
      }
      const children = region.children && region.children.length ? region.children : [];
      if (!children.length) {
        lines.push('    - empty: true');
        continue;
      }
      for (const line of renderNodeYamlLines(children, depthLimit, 2)) lines.push(line);
    }

    return lines.join('\n');
  };

  const options = modeToOptions(window.__simpleAriaMode || 'ai');
  const depth = window.__simpleAriaDepth ?? null;
  const refPrefix = window.__simpleAriaRefPrefix || '';
  const refMap = {};

  for (const el of document.querySelectorAll('[data-snapshot-ref]')) {
    el.removeAttribute('data-snapshot-ref');
  }

  const activeDialogElement = (() => {
    const dialogSelectors = [
      '[role="dialog"]',
      '[role="alertdialog"]',
      '.el-dialog',
      '.ant-modal',
      '.ivu-modal',
      '.heasec-modal',
      '.heasec-modal-overlay',
      '.heasec-confirm-overlay',
      '.modal',
      '.modal-container',
      '.modal-dialog',
      '.modal-content',
      '.drawer-content'
    ].join(',');
    const candidates = Array.from(document.querySelectorAll(dialogSelectors))
      .filter((el) => visibilityPass(el, options.visibility) && isVisible(el))
      .map((el) => {
        const bounds = rectOf(el);
        return {
          el,
          bounds,
          area: areaOf(bounds),
          actionableDescendants: Array.from(el.querySelectorAll(
            'button,a[href],input,select,textarea,summary,[role="button"],[role="link"],[tabindex]'
          )).filter((control) => isVisible(control) && receivesPointerEvents(control)).length,
          score: dialogRegionScore(el, bounds, Math.max(1, (window.innerWidth || 0) * (window.innerHeight || 0)))
        };
      })
      .filter((item) => item.area >= 1200);
    candidates.sort((a, b) => {
      if (b.actionableDescendants !== a.actionableDescendants)
        return b.actionableDescendants - a.actionableDescendants;
      if (b.score !== a.score) return b.score - a.score;
      return a.area - b.area;
    });
    return candidates[0]?.el || null;
  })();

  const canAssignRefInCurrentState = (el) => {
    if (!activeDialogElement)
      return true;
    return activeDialogElement === el || activeDialogElement.contains(el);
  };

  const walk = (el, curDepth, skipElements = null, inActionableAncestor = false) => {
    const tagName = (el.tagName || '').toLowerCase();
    if (tagName === 'script' || tagName === 'style' || tagName === 'noscript')
      return null;

    const visible = visibilityPass(el, options.visibility);
    if (!visible)
      return null;

    const roleAttr = (el.getAttribute('role') || '').toLowerCase();
    const role = roleAttr || implicitRole(el) || 'generic';
    if (!role || role === 'presentation' || role === 'none')
      return null;

    const interactive = isInteractive(el);
    const name = accName(el, role, interactive);
    const pointer = receivesPointerEvents(el);
    const cursor = cursorStyle(el);
    const baseActionable = isActionableElement(el, interactive, visible, pointer, cursor);
    const actionable = baseActionable && canAssignRefInCurrentState(el) && !(inActionableAncestor && role === 'generic');
    const props = getAriaStateProps(el, role);

    const children = [];
    const hasDepthLimit = depth !== null && depth !== undefined;
    if (!hasDepthLimit || curDepth < depth) {
      const includeTextChildren = !(name && isTextNameRole(role));
      for (const child of el.childNodes) {
        if (child.nodeType === Node.TEXT_NODE) {
          if (includeTextChildren) {
            const value = text(child.textContent);
            if (value) children.push(value);
          }
          continue;
        }
        if (child.nodeType === Node.ELEMENT_NODE) {
          if (skipElements && skipElements.has(child)) continue;
          const sub = walk(child, curDepth + 1, skipElements, inActionableAncestor || baseActionable);
          if (sub) children.push(sub);
        }
      }
    }

    const keep = interactive || !!name || role !== 'generic' || children.length > 0;
    if (!keep)
      return null;

    let ref = null;
    if (options.refs === 'all') {
      ref = ensureRef(el, role, name, refPrefix);
    } else if (options.refs === 'visible' && visible) {
      ref = ensureRef(el, role, name, refPrefix);
    } else if (options.refs === 'interactable' && actionable) {
      ref = ensureRef(el, role, name, refPrefix);
    }

    if (ref)
      refMap[ref] = '[data-snapshot-ref="' + ref + '"]';
    else if (el.hasAttribute('data-snapshot-ref'))
      el.removeAttribute('data-snapshot-ref');

    return {
      ref,
      role,
      name,
      interactive: actionable,
      receivesPointerEvents: pointer,
      active: document.activeElement === el,
      cursor,
      ...props,
      props,
      children
    };
  };

  const rootNode = walk(ROOT, 0);
  const tree = normalizeGenerics(rootNode ? [rootNode] : []);
  const collectRegions = () => {
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
    const viewportArea = Math.max(1, viewportWidth * viewportHeight);
    const candidates = [];
    const elements = Array.from(document.body.querySelectorAll('*'));

    for (const el of elements) {
      if (!visibilityPass(el, options.visibility) || !isVisible(el)) continue;
      const bounds = rectOf(el);
      const area = areaOf(bounds);
      if (area < 1200) continue;

      let type = regionType(el);
      const nearTop = bounds.y <= 12 && bounds.width >= viewportWidth * 0.55 && bounds.height <= Math.max(180, viewportHeight * 0.22);
      const leftRail = bounds.x <= 12 && bounds.height >= viewportHeight * 0.45 && bounds.width <= Math.max(420, viewportWidth * 0.32);
      const dominantMain = area >= viewportArea * 0.28 && bounds.width >= viewportWidth * 0.45;

      if (!type && nearTop) type = 'header';
      if (!type && leftRail) type = 'navigation';
      if (!type && dominantMain) type = 'main';
      if (!type) continue;

      let priority = {
        dialog: 100,
        main: 90,
        navigation: 82,
        header: 80,
        tablist: 75,
        form: 70,
        data: 68,
        section: 50,
        footer: 40
      }[type] || 20;
      if (type === 'dialog') {
        priority += dialogRegionScore(el, bounds, viewportArea);
      }

      candidates.push({ el, type, bounds, priority, area });
    }

    candidates.sort((a, b) => {
      if (b.priority !== a.priority) return b.priority - a.priority;
      if (a.type === 'dialog' && b.type === 'dialog') return a.area - b.area;
      return b.area - a.area;
    });

    const selected = [];
    for (const candidate of candidates) {
      if (selected.length >= 10) break;
      const duplicate = selected.some((item) => {
        if (item.el === candidate.el) return true;
        if (item.el.contains(candidate.el) && item.type === candidate.type) return true;
        if (candidate.el.contains(item.el) && item.type === candidate.type) return true;
        if (item.el.contains(candidate.el) || candidate.el.contains(item.el)) return false;
        return boundsOverlapRatio(item.bounds, candidate.bounds) > 0.92;
      });
      if (!duplicate) selected.push(candidate);
    }

    if (!selected.some((item) => item.type === 'main')) {
      selected.unshift({
        el: document.body,
        type: 'page',
        bounds: rectOf(document.body),
        priority: 10,
        area: areaOf(rectOf(document.body))
      });
    }

    return selected.map((candidate, index) => {
      const nestedCandidates = selected
        .map((item, itemIndex) => ({ ...item, itemIndex }))
        .filter((item) => item.el !== candidate.el && candidate.el.contains(item.el));
      const nestedRegions = nestedCandidates.map((item) => item.el);
      const skipElements = nestedRegions.length ? new Set(nestedRegions) : null;
      const node = walk(candidate.el, 0, skipElements);
      const normalized = normalizeGenerics(node ? [node] : []);
      let children = normalized.length === 1 && normalized[0] && typeof normalized[0] !== 'string'
        ? (normalized[0].children && normalized[0].children.length ? normalized[0].children : normalized)
        : normalized;
      const dataPreview = candidate.type === 'data' ? collectDataPreview(candidate.el) : null;
      if (candidate.type === 'data') {
        children = keepActionableNodes(normalized);
      }
      const regionNode = normalized.find((item) => item && typeof item !== 'string') || null;
      return {
        id: 'r' + (index + 1),
        type: candidate.type,
        label: regionLabel(candidate.el, candidate.type),
        bounds: candidate.bounds,
        summary: summarizeRegion(regionNode || { children }),
        child_regions: nestedCandidates.map((item) => ({
          id: 'r' + (item.itemIndex + 1),
          type: item.type,
          label: regionLabel(item.el, item.type)
        })),
        data_preview: dataPreview,
        children
      };
    });
  };

  const regions = collectRegions();
  const scriptUrls = (() => {
    const currentOrigin = window.location.origin;
    const candidates = [
      ...Array.from(document.scripts || []).map((script) => script.src || ''),
      ...performance.getEntriesByType('resource')
        .filter((entry) => String(entry.initiatorType || '').toLowerCase() === 'script')
        .map((entry) => String(entry.name || '')),
    ];
    const seen = new Set();
    const urls = [];
    for (const candidate of candidates) {
      if (!candidate || urls.length >= 80) continue;
      try {
        const resolved = new URL(candidate, window.location.href);
        if (resolved.origin !== currentOrigin || seen.has(resolved.href)) continue;
        seen.add(resolved.href);
        urls.push(resolved.href);
      } catch {}
    }
    return urls;
  })();
  const pageContext = {
    current_url: window.location.href,
    page_title: document.title || '',
    script_urls: scriptUrls,
    viewport: {
      width: window.innerWidth || document.documentElement.clientWidth || 0,
      height: window.innerHeight || document.documentElement.clientHeight || 0
    },
    focused_ref: document.activeElement &&
      document.activeElement !== document.body &&
      document.activeElement !== document.documentElement &&
      document.activeElement.getAttribute
      ? document.activeElement.getAttribute('data-snapshot-ref') || null
      : null
  };
  const yaml = renderRegionsYaml(regions, depth);

  return {
    page_context: pageContext,
    regions,
    tree,
    ref_map: refMap,
    yaml
  };
})();`;

function yamlString(value) {
  return JSON.stringify(String(value ?? ''));
}

async function buildBrowserContext(page, pageContext = {}) {
  const pages = page.context().pages();
  const activeIndex = Math.max(0, pages.indexOf(page));
  const tabs = await Promise.all(
    pages.map(async (candidate, index) => {
      let title = '';
      try {
        title = await candidate.title();
      } catch {
        title = '';
      }
      return {
        index: index + 1,
        title,
        url: candidate.url(),
        active: candidate === page
      };
    })
  );

  return {
    current_url: page.url(),
    page_title: pageContext.page_title || tabs[activeIndex]?.title || '',
    viewport: pageContext.viewport || null,
    focused_ref: pageContext.focused_ref || null,
    script_urls: Array.isArray(pageContext.script_urls) ? pageContext.script_urls : [],
    active_tab_index: activeIndex + 1,
    tabs
  };
}

function renderBrowserContextYaml(browserContext) {
  const lines = ['browser_context:'];
  lines.push(`  current_url: ${yamlString(browserContext.current_url)}`);
  lines.push(`  page_title: ${yamlString(browserContext.page_title)}`);
  if (browserContext.viewport) {
    lines.push(`  viewport: "${browserContext.viewport.width}x${browserContext.viewport.height}"`);
  }
  lines.push(`  active_tab_index: ${browserContext.active_tab_index || 1}`);
  lines.push(`  focused_ref: ${browserContext.focused_ref ? yamlString(browserContext.focused_ref) : 'null'}`);
  lines.push('  script_urls:');
  for (const url of browserContext.script_urls || []) {
    lines.push(`    - script_url: ${yamlString(url)}`);
  }
  lines.push('  tabs:');
  for (const tab of browserContext.tabs || []) {
    lines.push(`    - index: ${tab.index}`);
    lines.push(`      title: ${yamlString(tab.title)}`);
    lines.push(`      url: ${yamlString(tab.url)}`);
    lines.push(`      active: ${tab.active ? 'true' : 'false'}`);
  }
  return lines.join('\n');
}

export async function captureSnapshot(page, options = {}) {
  const mode = options.mode || 'ai';
  const depth = options.depth ?? null;
  const refPrefix = options.refPrefix || '';

  await page.evaluate(
    ({ mode, depth, refPrefix }) => {
      window.__simpleAriaMode = mode;
      window.__simpleAriaDepth = depth;
      window.__simpleAriaRefPrefix = refPrefix || '';
    },
    { mode, depth, refPrefix }
  );

  const snapshot = await page.evaluate(SNAPSHOT_SCRIPT);
  const browserContext = await buildBrowserContext(page, snapshot.page_context || {});
  return {
    ...snapshot,
    browser_context: browserContext,
    yaml: `${renderBrowserContextYaml(browserContext)}\n\n${snapshot.yaml || ''}`.trim()
  };
}
