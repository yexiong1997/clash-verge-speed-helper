"use strict";

// This module handles only the displayed catalogue; it never changes selections.
const RowModel = (() => {
  const columns = new Set(["name", "profile", "node_type", "state", "score", "speed", "latency", "jitter", "successes", "loss"]);
  const historyColumns = new Set([
    "rank", "name", "profile", "node_type", "long_term_score",
    "weighted_median_speed_bps", "weighted_median_latency_ms",
    "availability_rate", "total_count"
  ]);

  const isValid = row => row.eligible && row.score != null;
  const isTesting = row => row.state === "等待" || row.state === "测速中" || Boolean(row.live);
  const isFailed = row => ["失败", "超时", "已停止", "配置已变更", "下载未完成", "初筛无效"].includes(row.state) ||
                          (row.score != null && !row.eligible) || (row.speed != null && row.score == null);

  function bucketCounts(rows, profiles, queryText = "") {
    const selectedProfiles = new Set((profiles || []).filter(p => p.selected).map(p => p.uid));
    const query = (queryText || "").trim().toLocaleLowerCase();
    const counts = {all: 0, valid: 0, testing: 0, failed: 0};
    for (const row of (rows || [])) {
      if (!selectedProfiles.has(row.uid)) continue;
      if (query && !`${row.name} ${row.profile} ${row.node_type || ""} ${row.uid}`.toLocaleLowerCase().includes(query)) continue;
      counts.all += 1;
      if (isValid(row)) counts.valid += 1;
      if (isTesting(row)) counts.testing += 1;
      if (isFailed(row)) counts.failed += 1;
    }
    return counts;
  }

  function visibleRows(rows, profiles, options = {}) {
    const selectedProfiles = new Set(profiles.filter(p => p.selected).map(p => p.uid));
    const query = (options.query || "").trim().toLocaleLowerCase();
    const status = options.status || "all";
    const column = columns.has(options.sort) ? options.sort : "score";
    const ascending = options.ascending === true;
    const freezeOrder = options.freezeOrder === true && column === "score" && !ascending;
    const pinSelectedTop = options.pinSelectedTop === true;
    const filtered = rows.map((row, index) => ({row, index})).filter(({row}) => {
      if (!selectedProfiles.has(row.uid)) return false;
      if (query && !`${row.name} ${row.profile} ${row.node_type || ""} ${row.uid}`.toLocaleLowerCase().includes(query)) return false;
      if (status === "valid") return isValid(row);
      if (status === "testing") return isTesting(row);
      if (status === "failed") return isFailed(row);
      return true;
    });
    if (freezeOrder) {
      if (pinSelectedTop) {
        const top = [], rest = [];
        for (const item of filtered) {
          if (item.row.selected || isTesting(item.row)) top.push(item.row);
          else rest.push(item.row);
        }
        return top.concat(rest);
      }
      return filtered.map(item => item.row);
    }
    return filtered.sort((a, b) => {
      const value = row => {
        if (column === "score") return row.eligible ? row.score : null;
        if (column === "loss") return row.successes == null ? null : Math.max(0, 6 - row.successes);
        if (column === "node_type") return row.node_type ? String(row.node_type).toUpperCase() : null;
        return row[column];
      };
      const x = value(a.row), y = value(b.row);
      if (x == null && y != null) return 1;
      if (x != null && y == null) return -1;
      if (x == null) {
        if (column === "score") {
          const s1a = a.row.eligible && a.row.stage1_score != null ? a.row.stage1_score : null;
          const s1b = b.row.eligible && b.row.stage1_score != null ? b.row.stage1_score : null;
          if (s1a != null && s1b == null) return -1;
          if (s1a == null && s1b != null) return 1;
          if (s1a != null && s1b != null && s1a !== s1b) return s1b - s1a;
          const selA = a.row.selected === true, selB = b.row.selected === true;
          if (selA !== selB) return selA ? -1 : 1;
        }
        return a.index - b.index;
      }
      const difference = typeof x === "string" ? x.localeCompare(y) : x - y;
      return difference ? (ascending ? difference : -difference) : a.index - b.index;
    }).map(item => item.row);
  }

  function historyRows(summaries, profiles, options = {}) {
    const selectedProfiles = new Set(profiles.filter(p => p.selected).map(p => p.uid));
    const selectedOnly = options.selectedOnly === true;
    const query = (options.query || "").trim().toLocaleLowerCase();
    const column = historyColumns.has(options.sort) ? options.sort : "rank";
    const ascending = options.ascending === true;
    const list = (summaries || []).map((item, index) => ({item, index})).filter(({item}) => {
      if (selectedOnly && !selectedProfiles.has(item.uid)) return false;
      if (query) {
        const hay = `${item.name} ${item.profile} ${item.node_type || ""} ${item.uid}`.toLocaleLowerCase();
        if (!hay.includes(query)) return false;
      }
      return true;
    });
    if (column === "rank" && !ascending) return list.map(x => x.item);
    return list.sort((a, b) => {
      if (column === "rank") return ascending ? a.index - b.index : b.index - a.index;
      const val = item => {
        if (column === "weighted_median_latency_ms" || column === "weighted_median_speed_bps") {
          return item.success_count > 0 ? item[column] : null;
        }
        return item[column];
      };
      const x = val(a.item), y = val(b.item);
      if (x == null && y != null) return 1;
      if (x != null && y == null) return -1;
      if (x == null) return a.index - b.index;
      const diff = typeof x === "string" ? x.localeCompare(y) : x - y;
      return diff ? (ascending ? diff : -diff) : a.index - b.index;
    }).map(x => x.item);
  }

  return Object.freeze({visibleRows, historyRows, bucketCounts});
})();

if (typeof module !== "undefined" && module.exports) module.exports = RowModel;
