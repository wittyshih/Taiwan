(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const button = $('check');
  const status = $('status');
  const result = $('result');

  if (!button || !status || !result) return;

  const setStatus = (message, error = false) => {
    status.textContent = message;
    status.style.color = error ? '#c0392b' : '#14243a';
  };

  const fmt = n =>
    Number.isFinite(n) ? n.toFixed(2) : '資料不足';

  const parseNum = s =>
    Number(String(s ?? '').replace(/,/g, '').trim());

  const wait = ms =>
    new Promise(resolve => setTimeout(resolve, ms));

  const monthStart = (date, offset) =>
    new Date(date.getFullYear(), date.getMonth() + offset, 1);

  const ymd = date =>
    `${date.getFullYear()}${String(date.getMonth() + 1).padStart(2, '0')}01`;

  function parseDate(s) {
    const parts = String(s).trim().split('/').map(Number);

    if (
      parts.length !== 3 ||
      parts.some(n => !Number.isFinite(n))
    ) return null;

    const [year, month, day] = parts;

    const date = new Date(
      year < 1911 ? year + 1911 : year,
      month - 1,
      day
    );

    return Number.isNaN(date.getTime()) ? null : date;
  }

  async function getMonth(symbol, date) {
    const url =
      `https://www.twse.com.tw/exchangeReport/STOCK_DAY` +
      `?response=json&date=${ymd(date)}` +
      `&stockNo=${encodeURIComponent(symbol)}`;

    const controller = new AbortController();

    const timer = setTimeout(
      () => controller.abort(),
      12000
    );

    try {
      const response = await fetch(url, {
        signal: controller.signal,
        cache: 'no-store'
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const json = await response.json();

      if (
        json.stat !== 'OK' ||
        !Array.isArray(json.data)
      ) return [];

      return json.data.map(row => {
        const date = parseDate(row[0]);
        const volume = parseNum(row[1]);
        const high = parseNum(row[4]);
        const low = parseNum(row[5]);
        const close = parseNum(row[6]);

        if (
          !date ||
          ![volume, high, low, close].every(Number.isFinite)
        ) return null;

        return { date, volume, high, low, close };
      }).filter(Boolean);

    } finally {
      clearTimeout(timer);
    }
  }

  function aggregate(daily, mode) {
    const groups = new Map();

    for (const item of daily) {
      let key;

      if (mode === 'month') {
        key =
          `${item.date.getFullYear()}-` +
          `${item.date.getMonth() + 1}`;
      } else {
        const day = item.date.getDay();
        const friday = new Date(item.date);

        friday.setDate(
          friday.getDate() + 5 - day
        );

        key =
          `${friday.getFullYear()}-` +
          `${friday.getMonth() + 1}-` +
          `${friday.getDate()}`;
      }

      if (!groups.has(key)) {
        groups.set(key, { ...item });
      } else {
        const g = groups.get(key);

        g.high = Math.max(g.high, item.high);
        g.low = Math.min(g.low, item.low);
        g.close = item.close;
        g.volume += item.volume;
      }
    }

    return [...groups.values()];
  }

  function kd(bars, period) {
    if (bars.length < period) return null;

    let k = 50;
    let d = 50;

    for (let i = period - 1; i < bars.length; i++) {
      const window = bars.slice(
        i - period + 1,
        i + 1
      );

      const high = Math.max(
        ...window.map(x => x.high)
      );

      const low = Math.min(
        ...window.map(x => x.low)
      );

      const rsv = high === low
        ? 50
        : Math.max(
            0,
            Math.min(
              100,
              (bars[i].close - low) /
              (high - low) * 100
            )
          );

      k = (2 * k + rsv) / 3;
      d = (2 * d + k) / 3;
    }

    return {
      k,
      d,
      bullish: k > d
    };
  }

  function row(label, value) {
    if (!value) {
      return `<tr>
        <th>${label}</th>
        <td>資料不足</td>
        <td>—</td>
      </tr>`;
    }

    return `<tr>
      <th>${label}</th>
      <td>K ${fmt(value.k)} / D ${fmt(value.d)}</td>
      <td class="${value.bullish ? 'ok' : 'bad'}">
        ${value.bullish ? '多頭 ✓' : '非多頭 ✕'}
      </td>
    </tr>`;
  }

  button.addEventListener('click', async () => {
    const symbol = $('symbol').value.trim();
    const period = Number($('period').value);
    const threshold = Number($('ratio').value);

    if (
      !/^\d{4,6}$/.test(symbol) ||
      !Number.isInteger(period) ||
      period < 2 ||
      period > 100 ||
      !Number.isFinite(threshold) ||
      threshold < 0
    ) {
      setStatus(
        '請確認股票代號、RSV 週期與量比門檻。',
        true
      );
      return;
    }

    button.disabled = true;

    result.textContent =
      '正在讀取歷史資料，請稍候…';

    const now = new Date();

    const months = Math.max(
      48,
      Math.ceil(period * 1.8) + 12
    );

    const daily = [];

    try {
      for (let i = months - 1; i >= 0; i--) {
        const date = monthStart(now, -i);

        setStatus(
          `正在下載 ${symbol} 歷史資料：` +
          `${months - i}/${months} 個月份…\n` +
          '首次查詢可能需要數分鐘。'
        );

        const records = await getMonth(
          symbol,
          date
        );

        daily.push(...records);

        await wait(120);
      }

      const unique = [
        ...new Map(
          daily.map(item => [
            item.date.toISOString().slice(0, 10),
            item
          ])
        ).values()
      ].sort((a, b) => a.date - b.date);

      if (!unique.length) {
        throw new Error(
          '找不到此股票的歷史資料。'
        );
      }

      const day = kd(unique, period);

      const week = kd(
        aggregate(unique, 'week'),
        period
      );

      const month = kd(
        aggregate(unique, 'month'),
        period
      );

      const last = unique.at(-1);
      const prev = unique.at(-2);

      const ratio =
        prev && prev.volume > 0
          ? last.volume / prev.volume
          : null;

      const complete =
        day && week && month && ratio !== null;

      const pass =
        complete &&
        day.bullish &&
        week.bullish &&
        month.bullish &&
        ratio >= threshold;

      const verdict = !complete
        ? '資料不足，無法判定'
        : pass
          ? '符合三多＋量比條件 ✓'
          : '不符合全部條件';

      result.innerHTML = `
        <p>
          <strong>${symbol}</strong>
          ｜最新資料日
          ${last.date.toLocaleDateString('zh-TW')}
        </p>

        <div class="scroll">
          <table>
            <thead>
              <tr>
                <th>週期</th>
                <th>K / D</th>
                <th>判斷</th>
              </tr>
            </thead>
            <tbody>
              ${row('日 KD', day)}
              ${row('週 KD', week)}
              ${row('月 KD', month)}
            </tbody>
          </table>
        </div>

        <p>
          成交量比：
          <strong>
            ${ratio === null
              ? '資料不足'
              : fmt(ratio) + ' 倍'}
          </strong>
          （門檻 ${threshold} 倍）
        </p>

        <p class="${pass ? 'ok' : 'bad'}">
          <strong>${verdict}</strong>
        </p>
      `;

      setStatus(
        `查詢完成：共取得 ${unique.length} 個交易日。\n` +
        '注意：本工具不是即時行情。'
      );

    } catch (error) {
      const detail =
        error?.name === 'AbortError'
          ? '資料請求逾時'
          : error?.message || String(error);

      setStatus(
        `查詢失敗：${detail}\n` +
        '可能是證交所跨網域限制或請求頻率限制。',
        true
      );

      result.textContent =
        '尚無法計算，請勿將此結果視為符合選股條件。';

    } finally {
      button.disabled = false;
    }
  });
})();
