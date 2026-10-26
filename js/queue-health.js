/* 队列提示条：把"点了没反应"变成一句话。
 *
 * 背景：生成与批改都走"入队 → worker 取任务"。worker 没在跑时，接口照样 202，
 * 前端只能转圈，用户等十几分钟也看不出问题在哪。这个模块读 /api/v1/health 的
 * queue 字段，在排队过久或明显没人干活时，直接在页面顶部说清楚原因和下一步。
 *
 * 只读、只提示，不替用户重试：重启 worker 是运维动作，页面不该偷偷干。
 */
(function () {
  const A = App;
  const REFRESH_MS = 30000;

  function S() {
    return A.state.queueHealth = A.state.queueHealth || {
      data: null, at: 0, busy: false, failedAt: 0
    };
  }

  function minutes(seconds) {
    const total = Math.max(0, Math.round(seconds || 0));
    if (total < 60) return `${total} 秒`;
    return `${Math.floor(total / 60)} 分 ${total % 60} 秒`;
  }

  A.queueNotice = function () {
    const state = S();
    const now = Date.now();
    const stale = !state.at || now - state.at > REFRESH_MS;
    // 拿不到 health（老服务端没这个字段）时不要在每次渲染里重试打接口
    if (!state.busy && stale && now > state.failedAt) {
      state.busy = true;
      A.api.health()
        .then(payload => {
          state.data = (payload && payload.queue) || null;
          state.at = Date.now();
          state.busy = false;
          if (state.data) A.render();
        })
        .catch(() => {
          state.busy = false;
          state.failedAt = Date.now() + 60000;
        });
    }
    const queue = state.data;
    if (!queue) return '';

    const waiting = Number(queue.queued) || 0;
    const oldest = Number(queue.oldest_queued_seconds) || 0;
    if (!waiting) return '';

    if (queue.worker_alive === false) {
      const since = queue.since_last_claim_seconds;
      return `<div class="queue-notice bad">${A.icon('info')}
        <div><b>有 ${waiting} 个任务排在队里，后台没有在处理</b>
        <p>最近一次取任务是${since == null ? '从未有过' : ` ${minutes(since)}前`}，
        说明 worker 没在跑。任务不会自己开始，需要管理员执行：</p>
        <code>sudo systemctl restart whu-worker</code></div></div>`;
    }
    if (oldest > 120) {
      return `<div class="queue-notice warn">${A.icon('clock')}
        <div><b>已排队 ${minutes(oldest)}</b>
        <p>worker 在取任务，但这一件迟迟没完成——多半卡在模型调用上。看日志：
        <code>sudo journalctl -u whu-worker -n 40</code></p></div></div>`;
    }
    return '';
  };
})();
