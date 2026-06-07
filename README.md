# Polybot — Polymarket BTC 5dk "son saniye" botu (paper trading)

Polymarket'in 5 dakikalık **"Bitcoin Up or Down"** marketlerinde, **gerçek
parayla değil kağıt üzerinde** çalışan strateji botu. $100 sanal bütçeyle başlar.

## Strateji (araştırmaya dayalı)
BTC'yi 5 dakika önceden tahmin etmek ~yazı-turadır; onu yapmıyoruz. Bunun yerine:

1. Her 5dk pencerenin **son ~45 saniyesini** bekle. O ana kadar Chainlink
   oracle fiyatı sonucu neredeyse belirlemiştir.
2. **Brownian olasılık modeli:** `P(up) = Φ(move / σ_kalan)`
   - `move` = şu anki fiyat − pencere başı fiyatı ("price to beat")
   - `σ_kalan` = kalan saniyelerdeki $ volatilitesi (oracle tick'lerinden)
3. Polymarket'in ince 5dk order book'u **geç kaldığı** için, kazanması
   neredeyse kesin taraf hâlâ ucuz fiyatlı olabilir. `P(taraf) − ask > eşik`
   ise o tarafı al (paper).
4. Pozisyon büyüklüğü: **kesirli Kelly** (yarım Kelly, %5 bütçe tavanı).

## Fiyat & çözüm: %100 Polymarket-native
Hem sinyal hem çözüm, Polymarket'in **kendi Chainlink BTC/USD oracle'ından**
gelir (marketleri tam bununla settle ediyor) — public RTDS websocket üzerinden,
**borsa yok, API key yok**:
```
wss://ws-live-data.polymarket.com   topic: crypto_prices_chainlink (btc/usd)
```
Çözüm Polymarket gibi: pencere sonu fiyatı ≥ pencere başı fiyatı → "Up" kazanır.

## Dosyalar
| Dosya | Görev |
|-------|-------|
| `feed.py`   | Chainlink oracle fiyat feed'i (websocket, arka plan thread, auto-reconnect) |
| `paper.py`  | Ana döngü: pencere son saniyesinde karar + çözüm + paper PnL |
| `config.py` | Tüm ayarlar (timing, eşikler, Kelly, bütçe) |
| `status.py` | Sanal portföy durumu (bakiye, K/Z, kazanma oranı) |

## Kullanım
```
pip install -r requirements.txt
python paper.py                      # tek pencere (debug)
python paper.py --minutes 290 --autopush   # CI uzun döngü
python status.py                     # durum / bakiye
```

## Otomatik çalışma (GitHub Actions, 7/24)
`.github/workflows/paper.yml` her iş ~5 saat açık kalır; içinde pencere-pencere
çalışır, state'i (`bankroll.json`, `paper_log.csv`, `paper_pending.json`,
`heartbeat.json`) repo'ya push'lar. `heartbeat.json`'ın `utc` zamanı güncelse
bot canlıdır. Durumu görmek için repodaki `bankroll.json`'a bak.

## Sınırlar
- Hâlâ **kağıt üzerinde** — gerçek emir yok. Edge pozitif kanıtlanırsa cüzdan +
  API key konuşulur.
- Paper çözümü oracle fiyatıyla yapılır; Polymarket'in resmi sonucuyla %~birebir
  ama nadir uç durumlarda küçük sapma olabilir.
