"use strict";

// This module handles only the displayed catalogue; it never changes selections.
const RowModel = (() => {
  const columns = new Set(["name", "profile", "state", "score", "speed", "latency", "jitter", "successes"]);

  function visibleRows(rows, profiles, options = {}) {
    const selectedProfiles = new Set(profiles.filter(p => p.selected).map(p => p.uid));
    const query = (options.query || "").trim().toLocaleLowerCase();
    const status = options.status || "all";
    const column = columns.has(options.sort) ? options.sort : "score";
    const ascending = options.ascending === true;
    const isValid = row => row.eligible && row.score != null;
    const isTesting = row => row.state === "等待" || row.state === "测速中";
    const isFailed = row => ["失败", "超时", "已停止", "配置已变更", "下载未完成"].includes(row.state) ||
                            (row.score != null && !row.eligible) || (row.speed != null && row.score == null);
    return rows.map((row, index) => ({row, index})).filter(({row}) => {
      if (!selectedProfiles.has(row.uid)) return false;
      if (query && !`${row.name} ${row.profile} ${row.uid}`.toLocaleLowerCase().includes(query)) return false;
      if (status === "valid") return isValid(row);
      if (status === "testing") return isTesting(row);
      if (status === "failed") return isFailed(row);
      return true;
    }).sort((a, b) => {
      const value = row => column === "score" && !row.eligible ? null : row[column];
      const x = value(a.row), y = value(b.row);
      if (x == null && y != null) return 1;
      if (x != null && y == null) return -1;
      if (x == null) return a.index - b.index;
      const difference = typeof x === "string" ? x.localeCompare(y) : x - y;
      return difference ? (ascending ? difference : -difference) : a.index - b.index;
    }).map(item => item.row);
  }

  return Object.freeze({visibleRows});
})();

if (typeof module !== "undefined" && module.exports) module.exports = RowModel;
