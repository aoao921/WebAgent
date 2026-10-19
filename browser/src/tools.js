import { captureSnapshot } from './snapshotEngine.js';

export async function navigateTool(page, url) {
  if (!url) {
    throw new Error('navigateTool: url is required');
  }
  await page.goto(url, { waitUntil: 'domcontentloaded' });
}

export async function snapshotTool(page, options = {}) {
  return await captureSnapshot(page, options);
}

export function findRefByText(snapshot, text) {
  if (!snapshot || !text) return null;
  const queue = Array.isArray(snapshot.tree) ? [...snapshot.tree] : [snapshot.tree];

  while (queue.length) {
    const node = queue.shift();
    if (!node || typeof node === 'string') continue;
    const name = node.name || '';
    const placeholder = (node.props && node.props.placeholder) || '';
    if (name.includes(text) || placeholder.includes(text)) {
      return node.ref || null;
    }
    if (node.children) queue.push(...node.children);
  }

  return null;
}

export async function clickByRef(page, ref, refMap) {
  if (!ref) {
    throw new Error('clickByRef: ref is required');
  }
  if (!refMap || typeof refMap !== 'object') {
    throw new Error('clickByRef: refMap is required');
  }

  const selector = refMap[ref];
  if (!selector) {
    throw new Error(`clickByRef: unknown ref "${ref}"`);
  }

  await page.locator(selector).click();
}

export async function clickTool(page, ref, refMap) {
  await clickByRef(page, ref, refMap);
}

export async function dblclickByRef(page, ref, refMap) {
  if (!ref) {
    throw new Error('dblclickByRef: ref is required');
  }
  if (!refMap || typeof refMap !== 'object') {
    throw new Error('dblclickByRef: refMap is required');
  }

  const selector = refMap[ref];
  if (!selector) {
    throw new Error(`dblclickByRef: unknown ref "${ref}"`);
  }

  await page.locator(selector).dblclick();
}

export async function dblclickTool(page, ref, refMap) {
  await dblclickByRef(page, ref, refMap);
}

export async function fillByRef(page, ref, refMap, value) {
  if (!ref) {
    throw new Error('fillByRef: ref is required');
  }
  if (!refMap || typeof refMap !== 'object') {
    throw new Error('fillByRef: refMap is required');
  }

  const selector = refMap[ref];
  if (!selector) {
    throw new Error(`fillByRef: unknown ref "${ref}"`);
  }

  await page.locator(selector).fill(String(value ?? ''));
}

export async function fillTool(page, ref, refMap, value) {
  await fillByRef(page, ref, refMap, value);
}

export async function pressTool(page, key) {
  if (!key) {
    throw new Error('pressTool: key is required');
  }
  await page.keyboard.press(key);
}

export async function readTextTool(page, selector) {
  if (!selector) {
    throw new Error('readTextTool: selector is required');
  }
  return await page.locator(selector).innerText();
}
