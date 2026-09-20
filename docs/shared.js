/* ==========================================================================
   Paper Trading — ORTAK yardımcılar (docs/index.html + docs/positions.html)
   ==========================================================================
   Biçimlendirme ölçümün bir parçasıdır, süs değil: aynı sayının iki sayfada
   farklı yuvarlanması (ya da birinde "—", diğerinde "0.00" görünmesi) okuyucuya
   iki ayrı ölçüm gibi gelir. Bu yüzden biçimleyiciler, çıkış sebebi etiketleri ve
   renk/kimlik ataması BURADA tek kopya durur.

   Klasik script (modül değil): iki sayfa da `<script src="shared.js">` ile yükler
   ve buradaki `const`lar sonraki satır içi script'ten görünür. Build adımı, bundler
   ve CDN yok — GitHub Pages dosyayı olduğu gibi sunar.
   ========================================================================== */
"use strict";

/* ------------------------------------------------------------------ *
 * Biçimlendirme — tanımsız metrik "—" olur, 0 DEĞİL.
 * JSON'da nan null'dır (bkz. main.py::_jsonable): "ölçülemedi" ile
 * "ölçüldü, sıfır çıktı" aynı hücreye yazılmaz.
 * ------------------------------------------------------------------ */
const DASH = "—";
const num = (v) => (v === null || v === undefined || v === "" || Number.isNaN(v)) ? null : (Number.isFinite(Number(v)) ? Number(v) : null);
const fmt = (v, d = 2) => { const n = num(v); return n === null ? DASH : n.toFixed(d); };
const fmtSigned = (v, d = 2) => { const n = num(v); return n === null ? DASH : (n > 0 ? "+" : "") + n.toFixed(d); };
const pct = (v, d = 1) => { const n = num(v); return n === null ? DASH : (n * 100).toFixed(d) + "%"; };
const pctSigned = (v, d = 2) => { const n = num(v); return n === null ? DASH : (n > 0 ? "+" : "") + (n * 100).toFixed(d) + "%"; };
const pctRaw = (v, d = 2) => { const n = num(v); return n === null ? DASH : n.toFixed(d) + "%"; };
const usd = (v, d = 2) => { const n = num(v); return n === null ? DASH : (n > 0 ? "+" : "") + n.toFixed(d); };
const signClass = (v) => { const n = num(v); return n === null ? "" : (n > 0 ? "pos" : (n < 0 ? "neg" : "")); };
/* Ortalama R'nin bootstrap aralığı. Tek bir sınır bile okunamıyorsa "—": yarım bir
   aralık, tam bir aralık gibi okunurdu. Bir KAPI değildir (kabul çıtası bu sayıya
   bakmaz), yalnızca "bu ortalama ne kadar konuşabiliyor" sorusunun cevabıdır. */
const ciBand = (lo, hi, d = 2) => {
  const a = num(lo), b = num(hi);
  return (a === null || b === null) ? DASH : "[" + a.toFixed(d) + ", " + b.toFixed(d) + "]";
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));
/* Fiyat ve miktar ondalığı sembole göre değişir: BTC 77222.69, bir altcoin 0.7232.
   Sabit iki hane küçük fiyatları "0.72" diye yuvarlayıp stop ile girişi aynı sayı
   gösterirdi — stop mesafesi de o iki sayının farkından okunuyor. */
const price = (v) => {
  const n = num(v); if (n === null) return DASH;
  const a = Math.abs(n);
  return n.toFixed(a >= 1000 ? 2 : a >= 100 ? 3 : a >= 1 ? 4 : a >= 0.01 ? 6 : 8);
};
const qty = (v) => {
  const n = num(v); if (n === null) return DASH;
  const a = Math.abs(n);
  return n.toFixed(a >= 1000 ? 2 : a >= 1 ? 3 : 6);
};
/* Tüm zamanlar UTC'dir: defter UTC yazar ve `as_of` UTC'dir (kural 12). Tarayıcının
   yerel saatine çevirmek, okuyucunun barı defterdeki satırla eşleştirememesi demekti. */
const shortTs = (s) => {
  if (!s) return DASH;
  const d = new Date(s);
  if (Number.isNaN(+d)) return String(s).slice(0, 16).replace("T", " ");
  return d.toISOString().slice(5, 16).replace("T", " ");
};
const fullTs = (s) => {
  if (!s) return DASH;
  const d = new Date(s);
  if (Number.isNaN(+d)) return String(s).slice(0, 19).replace("T", " ");
  return d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
};
/* Filtrelenebilir gün anahtarı (YYYY-MM-DD), yine UTC. */
const dayKey = (s) => {
  if (!s) return "";
  const d = new Date(s);
  return Number.isNaN(+d) ? String(s).slice(0, 10) : d.toISOString().slice(0, 10);
};
const el = (id) => document.getElementById(id);

/* Çıkış sebebi deftere kod olarak yazılır; sayfa onu okunur hâle getirir ama
   likidasyonu DİĞERLERİYLE AYNI renkte göstermez: likidasyon bir stop değil,
   bakım marjının ihlalidir (kural 13) ve raporun görmesi gereken şey tam olarak o. */
const EXIT_LABELS = { stop: "stop", tp: "hedef (TP)", partial: "kısmi çıkış", liquidation: "likidasyon", signal: "sinyal" };
const exitLabel = (code) => EXIT_LABELS[code] || (code || DASH);
const exitClass = (code) => (code === "liquidation" ? "neg" : "");

/* Çıkışın ALT sebebi (core/portfolio.py::_exit_notes -> `exit_rule=` etiketi).
   `exit_reason` beş kaba koddur: "stop" hem ilk stop'u hem takip eden stop'u,
   "signal" hem zaman stop'unu hem başka bir strateji çıkışını anlatır. Yönetimin
   katkısı tam olarak bu ayrımda ölçülür (modeller 13/14/15), bu yüzden alt sebep
   varsa çıkış etiketi onu söyler. */
const EXIT_RULE_LABELS = {
  trailing_atr: "takip eden stop (ATR)",
  giveback: "takip eden stop (geri verme)",
  breakeven: "başabaş stop'u",
  partial: "kısmi çıkış stop'u",
  time_stop: "zaman stop'u",
};
function exitText(trade) {
  const rule = trade && trade.exit_rule ? String(trade.exit_rule) : "";
  const base = exitLabel(trade ? trade.exit_reason : "");
  if (!rule) return base;
  return EXIT_RULE_LABELS[rule] || (base + " (" + rule + ")");
}

/* ------------------------------------------------------------------ *
 * Renk ataması: kimlik sabittir. Bir model filtrelendiğinde ya da
 * sıralama değiştiğinde hiçbir modelin rengi DEĞİŞMEZ — rengi sıraya
 * göre dağıtmak, "trend maviydi" diye öğrenen okuyucuyu yanıltır.
 * ------------------------------------------------------------------ */
const SLOTS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
const REF_COLOR = "#c3c2b7";      // referans çıpası: nötr, hue yok — bir fikir değil zemin
const CTRL_COLOR = "#898781";     // kontrol grubu: nötr, daha sönük

function assignStyles(models, benchmarks, controlModel) {
  const style = {};
  let slot = 0;
  for (const model of models) {
    if ((benchmarks || []).includes(model)) {
      style[model] = { color: REF_COLOR, dash: "", width: 2.5, kind: "reference" };
    } else if (model === controlModel) {
      style[model] = { color: CTRL_COLOR, dash: "5 4", width: 2, kind: "control" };
    } else {
      // 8 slot dolduğunda yeni renk ÜRETİLMEZ: slot baştan alınır ama çizgi kesikli olur,
      // yani kimliği renk + desen birlikte taşır (renk körlüğünde tek başına renk yetmez).
      const cycle = Math.floor(slot / SLOTS.length);
      style[model] = {
        color: SLOTS[slot % SLOTS.length],
        dash: cycle === 0 ? "" : "7 3",
        width: 2,
        kind: "competitor",
      };
      slot += 1;
    }
  }
  return style;
}

/* Yük her turda yeniden yazılır ve workflow onu commit eder: `no-store` + zaman
   damgası olmadan tarayıcı bir önceki turun JSON'unu gösterirdi. */
function fetchPayload(path) {
  return fetch(path + "?t=" + Date.now(), { cache: "no-store" })
    .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
}

/* Yön rozeti — iki sayfada da aynı görünür. */
const dirBadge = (direction) =>
  '<span class="dir ' + esc(direction) + '">' + esc(String(direction || "").toUpperCase()) + "</span>";

/* ------------------------------------------------------------------ *
 * Ölçüm okuma — TEK yol.
 *
 * Ortalama R, kazanma oranı ve işlem sayısı `core/metrics.py`nin
 * ürettiği sayılardır; sayfa onları ÇİZER, yeniden HESAPLAMAZ (kural 7).
 * İki hesap yolu bugün hizalansa bile yarın ayrışır: birinin kısmi
 * çıkışı sayması, diğerinin saymaması yeter ve aynı model iki sayfada
 * iki farklı kazanma oranı gösterir — okuyucuya iki ayrı ölçüm gibi
 * gelir (bkz. docs/shared.css'in tek kopya olma gerekçesi).
 *
 * `model` null ise HAVUZ okunur (`pooled`): yalnızca yarışmacılar,
 * çünkü çıpanın R'si yoktur (kural 15) ve kopyanınki başka birimdedir
 * (kural 15b). Kapsam çağıranın söylemesi gereken bir şeydir; bu yüzden
 * dönen nesne `scope` alanını da taşır.
 * ------------------------------------------------------------------ */
function statsSlice(payload, model, direction) {
  const dir = direction && direction !== "all" ? direction : "total";
  if (!payload) return null;
  if (!model) {
    const pooled = payload.pooled || {};
    const stats = (pooled.directions || {})[dir];
    return stats ? { ...stats, scope: "pooled", models: pooled.models || [] } : null;
  }
  const row = (payload.models || []).find((m) => (m.model || m.name) === model);
  return row && row[dir] ? { ...row[dir], scope: "model", models: [model] } : null;
}
