/* DEPRECATED: legacy browser-side text extraction. The active UI uses /api/document/extract. */
window.StudywellImport = (() => {
  const MAX_FILE = 32 * 1024 * 1024;
  const MAX_XML = 8 * 1024 * 1024;
  const MAX_TEXT = 100000;
  const descendants = (node, name) => Array.from(node.getElementsByTagNameNS('*', name));
  function parseXml(text) {
    const doc = new DOMParser().parseFromString(text, 'application/xml');
    if (descendants(doc, 'parsererror').length || /<!DOCTYPE/i.test(text)) throw new Error('This document contains unreadable XML. Try saving a fresh copy in Word or PowerPoint.');
    return doc;
  }
  function paragraphs(doc) {
    return descendants(doc, 'p').map(p => {
      const walk = n => {
        if (n.nodeType !== 1) return '';
        if (n.localName === 't') return n.textContent;
        if (n.localName === 'tab') return '\t';
        if (n.localName === 'br' || n.localName === 'cr') return '\n';
        return Array.from(n.childNodes).map(walk).join('');
      };
      return walk(p).trim();
    }).filter(Boolean).join('\n');
  }
  async function read(file) {
    const ext = file.name.split('.').pop().toLowerCase();
    if (['doc', 'ppt'].includes(ext)) throw new Error('Please save this older Office file as .docx or .pptx, then upload it again.');
    if (!['txt', 'md', 'docx', 'pptx'].includes(ext)) throw new Error('Choose a .docx, .pptx, .txt, or .md file.');
    if (file.size > MAX_FILE) throw new Error('Please use a file no larger than 32 MB.');
    let text = '', units = null;
    if (ext === 'txt' || ext === 'md') text = await file.text();
    else {
      let zip;
      try { zip = await JSZip.loadAsync(await file.arrayBuffer()); }
      catch { throw new Error('This Office file could not be opened. It may be damaged or password-protected. Save an unprotected .docx or .pptx copy and try again.'); }
      let total = 0;
      async function xml(path, optional = false) {
        const entry = zip.file(path);
        if (!entry) { if (optional) return null; throw new Error('This file is missing required document content. Save a fresh Office copy and try again.'); }
        const size = entry._data?.uncompressedSize;
        if (!Number.isFinite(size) || size > MAX_XML || (total += size) > 32 * 1024 * 1024) throw new Error('This document is too large to process. Split it into smaller study modules.');
        const raw = await entry.async('string');
        if (raw.length > MAX_XML) throw new Error('This document is too large to process. Split it into smaller study modules.');
        return parseXml(raw);
      }
      if (ext === 'docx') text = paragraphs(await xml('word/document.xml'));
      else {
        const presentation = await xml('ppt/presentation.xml');
        const rels = await xml('ppt/_rels/presentation.xml.rels');
        const targets = new Map(descendants(rels, 'Relationship').filter(r => r.getAttribute('TargetMode') !== 'External').map(r => [r.getAttribute('Id'), r.getAttribute('Target')]));
        const slides = descendants(presentation, 'sldId');
        if (slides.length > 500) throw new Error('Please split presentations longer than 500 slides into smaller modules.');
        const parts = [];
        for (const [i, slide] of slides.entries()) {
          const rid = Array.from(slide.attributes).find(a => a.localName === 'id' && a.namespaceURI)?.value;
          const target = targets.get(rid);
          if (!target) throw new Error('The slide order could not be read. Save a fresh PowerPoint copy and try again.');
          const segments = (target.startsWith('/') ? target.slice(1) : 'ppt/' + target).split('/');
          const normalized = [];
          for (const segment of segments) { if (segment === '..') normalized.pop(); else if (segment && segment !== '.') normalized.push(segment); }
          const slideText = paragraphs(await xml(normalized.join('/')));
          if (slideText) parts.push('Slide ' + (i + 1) + '\n' + slideText);
          if (parts.reduce((n, p) => n + p.length, 0) > MAX_TEXT) throw new Error('This material exceeds 100,000 characters. Split it into smaller study modules.');
        }
        units = slides.length;
        text = parts.join('\n\n');
      }
    }
    text = text.replace(/\r\n?/g, '\n').trim();
    if (!text) throw new Error('No readable text was found. Image-only pages or slides need their text pasted manually.');
    if (text.length > MAX_TEXT) throw new Error('This material exceeds 100,000 characters. Split it into smaller study modules.');
    return { text, units, ext };
  }
  return { read };
})();
