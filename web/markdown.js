/* 轻量 Markdown 渲染器（无第三方依赖）。
 *
 * 支持：标题、段落与软换行、粗体/斜体/删除线、行内代码、围栏代码块、
 * 无序/有序/嵌套列表、任务列表、引用、表格、分隔线、链接、图片、自动链接。
 *
 * 安全策略：先对全部原文做 HTML 转义，再只插入本模块生成的标签；
 * 链接/图片协议做白名单校验，因此模型输出里的原始 HTML 不会被当成标签执行。
 *
 * 浏览器里挂到 window.FoolMarkdown；Node 里可直接 require（测试用）。
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.FoolMarkdown = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const HTML_ESCAPES = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  };

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch]);
  }

  function decodeEntities(text) {
    return String(text)
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'");
  }

  /** 只放行常见安全协议；未知协议（javascript:/data:/vbscript:）一律拒绝。 */
  function sanitizeUrl(url) {
    const value = String(url).trim();
    if (!value) return "";
    if (/^(https?:|mailto:)/i.test(value)) return value;
    if (/^[a-z][a-z0-9+.-]*:/i.test(value)) return "";
    return value; // 相对路径、锚点
  }

  function dedent(line, amount) {
    let count = 0;
    while (count < amount && line[count] === " ") count++;
    return line.slice(count);
  }

  // ── 行内渲染 ────────────────────────────────────────────
  // 用 NUL 占位符保护代码、链接、转义字符，避免后续的 * _ ~ 规则误伤。
  const PLACEHOLDER_RE = /\u0000(\d+)\u0000/g;

  function formatInline(raw) {
    const stash = [];
    const keep = (html) => {
      stash.push(html);
      return `\u0000${stash.length - 1}\u0000`;
    };

    let text = escapeHtml(raw);

    // 反斜杠转义：\*、\_ 等保持字面量
    text = text.replace(/\\([\\`*_{}\[\]()#+\-.!~|>])/g, (_, ch) =>
      keep(escapeHtml(ch))
    );

    // 行内代码
    text = text.replace(/(`+)([\s\S]*?)\1/g, (_, _ticks, code) => {
      let value = code;
      if (
        value.length > 2 &&
        value.startsWith(" ") &&
        value.endsWith(" ") &&
        value.trim()
      ) {
        value = value.slice(1, -1);
      }
      return keep(`<code>${value}</code>`);
    });

    // 图片
    text = text.replace(
      /!\[([^\]]*)\]\(([^)\s]+)(?:\s+["']([^"']*)["'])?\)/g,
      (_, alt, url, title) => {
        const safe = sanitizeUrl(decodeEntities(url));
        if (!safe) return keep(alt);
        const titleAttr = title ? ` title="${title}"` : "";
        return keep(
          `<img src="${escapeHtml(safe)}" alt="${alt}"${titleAttr} loading="lazy">`
        );
      }
    );

    // 链接
    text = text.replace(
      /\[([^\]]+)\]\(([^)\s]+)(?:\s+["']([^"']*)["'])?\)/g,
      (_, label, url, title) => {
        const safe = sanitizeUrl(decodeEntities(url));
        if (!safe) return keep(label);
        const titleAttr = title ? ` title="${title}"` : "";
        return keep(
          `<a href="${escapeHtml(safe)}" target="_blank" rel="noopener noreferrer"${titleAttr}>${label}</a>`
        );
      }
    );

    // 自动链接 <https://example.com>
    text = text.replace(/&lt;(https?:\/\/[^\s]+?)&gt;/g, (_, url) =>
      keep(
        `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`
      )
    );

    // 粗体、删除线、斜体（要求开头非空白，避免把 "2 * 3 * 4" 当强调）
    text = text.replace(/\*\*(\S[^*\n]*?)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/__(\S[^_\n]*?)__/g, "<strong>$1</strong>");
    text = text.replace(/~~(\S[^~\n]*?)~~/g, "<del>$1</del>");
    text = text.replace(/(^|[^*])\*(\S[^*\n]*?)\*(?!\*)/g, "$1<em>$2</em>");
    text = text.replace(/(^|[^\w])_(\S[^_\n]*?)_(?!\w)/g, "$1<em>$2</em>");

    // 段落内的软换行；占位符里没有换行，不会破坏代码块
    text = text.replace(/\n/g, "<br>");

    // 还原占位符（占位符里可能嵌套占位符，循环几轮）
    for (let guard = 0; guard < 10 && text.indexOf("\u0000") !== -1; guard++) {
      text = text.replace(PLACEHOLDER_RE, (_, index) => stash[Number(index)] ?? "");
    }
    return text;
  }

  // ── 块级渲染 ────────────────────────────────────────────

  function isTableDelimiter(line) {
    const cells = splitTableRow(line);
    return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
  }

  function splitTableRow(row) {
    let text = row.trim();
    if (text.startsWith("|")) text = text.slice(1);
    if (text.endsWith("|") && !text.endsWith("\\|")) text = text.slice(0, -1);

    const cells = [];
    let current = "";
    let inCode = false;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (ch === "\\" && text[i + 1] === "|") {
        current += "|";
        i++;
        continue;
      }
      if (ch === "`") {
        inCode = !inCode;
        current += ch;
        continue;
      }
      if (ch === "|" && !inCode) {
        cells.push(current.trim());
        current = "";
        continue;
      }
      current += ch;
    }
    cells.push(current.trim());
    return cells;
  }

  function isBlockStart(line, nextLine) {
    if (/^\s{0,3}(`{3,}|~{3,})/.test(line)) return true;
    if (/^\s{0,3}#{1,6}\s+/.test(line)) return true;
    if (/^\s{0,3}>/.test(line)) return true;
    if (/^\s{0,3}(?:([-*_])\s*){3,}$/.test(line)) return true;
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) return true;
    if (line.includes("|") && nextLine && isTableDelimiter(nextLine)) return true;
    return false;
  }

  function renderCodeFence(lines, start, match) {
    const fence = match[1];
    const marker = fence[0];
    const minLength = fence.length;
    const info = (match[2] || "").trim();
    const lang = info.split(/\s+/)[0] || "";
    const closing = new RegExp(
      `^\\s{0,3}${marker}{${minLength},}\\s*$`
    );
    const body = [];
    let i = start + 1;
    while (i < lines.length && !closing.test(lines[i])) {
      body.push(lines[i]);
      i++;
    }
    if (i < lines.length) i++;

    const langLabel = lang ? `<div class="codelang">${escapeHtml(lang)}</div>` : "";
    return {
      html: `<div class="codeblock">${langLabel}<pre><code>${escapeHtml(
        body.join("\n")
      )}</code></pre></div>`,
      next: i,
    };
  }

  function renderTable(lines, start) {
    const header = splitTableRow(lines[start]);
    const delimiter = splitTableRow(lines[start + 1]);
    if (header.length !== delimiter.length) return null;

    const aligns = delimiter.map((cell) => {
      const left = cell.startsWith(":");
      const right = cell.endsWith(":");
      if (left && right) return "center";
      if (right) return "right";
      if (left) return "left";
      return "";
    });
    const alignAttr = (index) =>
      aligns[index] ? ` style="text-align:${aligns[index]}"` : "";

    let i = start + 2;
    const rows = [];
    while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
      rows.push(splitTableRow(lines[i]));
      i++;
    }

    let html = '<div class="table-wrap"><table><thead><tr>';
    header.forEach((cell, index) => {
      html += `<th${alignAttr(index)}>${formatInline(cell)}</th>`;
    });
    html += "</tr></thead><tbody>";
    for (const row of rows) {
      html += "<tr>";
      header.forEach((_, index) => {
        const cell = row[index] === undefined ? "" : row[index];
        html += `<td${alignAttr(index)}>${formatInline(cell)}</td>`;
      });
      html += "</tr>";
    }
    html += "</tbody></table></div>";
    return { html, next: i };
  }

  function renderListItem(itemLines) {
    const lines = itemLines.slice();
    while (lines.length && !lines[0].trim()) lines.shift();
    while (lines.length && !lines[lines.length - 1].trim()) lines.pop();

    let prefix = "";
    let task = false;
    if (lines.length) {
      const match = lines[0].match(/^\[([ xX])\]\s*(.*)$/);
      if (match) {
        task = true;
        const checked = match[1].toLowerCase() === "x" ? " checked" : "";
        prefix = `<input type="checkbox" disabled${checked}> `;
        lines[0] = match[2];
      }
    }
    if (!lines.length) return { html: prefix, task };

    let body = renderBlocks(lines);
    // 列表项的第一段去掉 <p> 包裹，保持紧凑（后面可能还跟着嵌套列表/代码块）
    if (body.startsWith("<p>")) {
      const end = body.indexOf("</p>");
      if (end !== -1) {
        body = body.slice(3, end) + body.slice(end + 4);
      }
    }
    return { html: prefix + body, task };
  }

  function renderList(lines, start) {
    const first = lines[start].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    const baseIndent = first[1].length;
    const ordered = /^\d/.test(first[2]);
    const startNumber = ordered ? parseInt(first[2], 10) : 1;
    const contentIndent = baseIndent + first[2].length + 1;
    const items = [];
    let i = start;

    const itemAtLevel = (line) => {
      const match = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
      if (!match) return null;
      if (match[1].length !== baseIndent) return null;
      if (/^\d/.test(match[2]) !== ordered) return null;
      return match;
    };

    while (i < lines.length) {
      if (!lines[i].trim()) {
        // 空行后面还是同级列表项时，跳过空行继续
        let j = i;
        while (j < lines.length && !lines[j].trim()) j++;
        if (j < lines.length && itemAtLevel(lines[j])) {
          i = j;
          continue;
        }
        break;
      }

      const match = itemAtLevel(lines[i]);
      if (!match) break;

      const itemLines = [match[3]];
      i++;

      while (i < lines.length) {
        const next = lines[i];

        if (!next.trim()) {
          let j = i;
          while (j < lines.length && !lines[j].trim()) j++;
          if (j >= lines.length) {
            i = j;
            break;
          }
          const followingIndent = lines[j].length - lines[j].trimStart().length;
          if (followingIndent > baseIndent) {
            itemLines.push("");
            i++;
            continue;
          }
          break;
        }

        if (itemAtLevel(next)) break; // 下一个同级项

        const indent = next.length - next.trimStart().length;
        if (indent < baseIndent) break;
        if (indent <= baseIndent) {
          // 同级但不是列表项：当作懒续行；遇到新的块则结束本项
          if (isBlockStart(next, lines[i + 1])) break;
          itemLines.push(next.trim());
          i++;
          continue;
        }

        itemLines.push(dedent(next, contentIndent));
        i++;
      }

      items.push(itemLines);
    }

    const openTag =
      ordered && startNumber !== 1 ? `<ol start="${startNumber}">` : ordered ? "<ol>" : "<ul>";
    let html = openTag;
    for (const itemLines of items) {
      const item = renderListItem(itemLines);
      html += `<li${item.task ? ' class="task"' : ""}>${item.html}</li>`;
    }
    html += ordered ? "</ol>" : "</ul>";
    return { html, next: i };
  }

  function renderBlocks(lines) {
    let html = "";
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) {
        i++;
        continue;
      }

      // 围栏代码块
      const fence = line.match(/^\s{0,3}(`{3,}|~{3,})[ \t]*([^\n]*)$/);
      if (fence) {
        const result = renderCodeFence(lines, i, fence);
        html += result.html;
        i = result.next;
        continue;
      }

      // 标题
      const heading = line.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/);
      if (heading) {
        const level = heading[1].length;
        html += `<h${level}>${formatInline(heading[2])}</h${level}>`;
        i++;
        continue;
      }

      // 分隔线
      if (/^\s{0,3}(?:([-*_])\s*){3,}$/.test(line)) {
        html += "<hr>";
        i++;
        continue;
      }

      // 引用
      if (/^\s{0,3}>/.test(line)) {
        const quoted = [];
        while (i < lines.length && /^\s{0,3}>/.test(lines[i])) {
          quoted.push(lines[i].replace(/^\s{0,3}>\s?/, ""));
          i++;
        }
        html += `<blockquote>${renderBlocks(quoted)}</blockquote>`;
        continue;
      }

      // 表格
      if (line.includes("|") && i + 1 < lines.length && isTableDelimiter(lines[i + 1])) {
        const table = renderTable(lines, i);
        if (table) {
          html += table.html;
          i = table.next;
          continue;
        }
      }

      // 列表
      if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
        const result = renderList(lines, i);
        html += result.html;
        i = result.next;
        continue;
      }

      // 段落
      const paragraph = [line];
      let j = i + 1;
      while (
        j < lines.length &&
        lines[j].trim() &&
        !isBlockStart(lines[j], lines[j + 1])
      ) {
        paragraph.push(lines[j]);
        j++;
      }
      html += `<p>${formatInline(paragraph.join("\n"))}</p>`;
      i = j;
    }

    return html;
  }

  function renderMarkdown(source) {
    if (source === null || source === undefined) return "";
    const normalized = String(source).replace(/\r\n?/g, "\n");
    return renderBlocks(normalized.split("\n"));
  }

  return { renderMarkdown };
});
