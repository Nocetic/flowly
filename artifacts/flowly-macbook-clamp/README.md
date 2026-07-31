# Flowly Fit Clip

MacBook'un yalnızca alt kasasının ön kenarına geçen minimal TPU mandal. Dışarıdan
yalnız ince, dikey, logolu kapsül görünür. Ekran bu kapsülün üstündeki küçük temas
boncuğuna oturur; iki ince diyagonal kaburga MacBook'un içine doğru inerek açık
bir dik üçgen oluşturur.

## Resmî Apple ölçü zarfı

| Güncel model | Kapalı yükseklik | Derinlik |
|---|---:|---:|
| 13 inç MacBook Air | 11,3 mm | 215,0 mm |
| 15 inç MacBook Air | 11,5 mm | 237,6 mm |
| 14 inç MacBook Pro | 15,5 mm | 221,2 mm |
| 16 inç MacBook Pro | 16,8 mm | 248,1 mm |

Kaynaklar: [MacBook Air teknik özellikleri](https://www.apple.com/ca/macbook-air/specs/)
ve [MacBook Pro teknik özellikleri](https://www.apple.com/macbook-pro/specs/).

Apple, alt kasanın ön dudak kalınlığını ayrı bir ölçü olarak yayımlamıyor. Bu
yüzden çene açıklığı resmî toplam yüksekliklerden bire bir çıkarılmış bir Apple
ölçüsü değildir; farklı kasalara uyum için esnek TPU dil ve genişleyen açıklık
olarak tasarlanmış bir mühendislik toleransıdır. Apple da aksesuar prototiplerinin
gerçek cihazlarla sınanmasını önerir: [Designing Accessories](https://developer.apple.com/accessories/).

## Revize geometri

- Logolu dış uç: **10 × 18 mm**, yalnız **5 mm** derinlik
- MacBook içine uzanma: **25 mm**
- Ekran teması: logolu uçta yaklaşık **6,6 × 1,0 mm** yumuşak boncuk
- Alt çene: **8,5 × 25 × 1,0 mm**
- Esnek üst dil: **6 × 23 × 1,6 mm**
- Nominal çene açıklığı: önde **4,0 mm**, iç uçta **5,8 mm**
- Oyma Flowly logosu: yaklaşık **5,5 mm görünür yükseklik**
- İç yapı: iki adet **1,2 mm** diyagonal yan kaburga; merkez tamamen açık

## Baskı ve güvenlik

- Malzeme: **TPU 95A veya 98A**; PLA/PETG kullanmayın.
- Katman: 0,20 mm; 3 duvar; %20–25 gyroid dolgu.
- İlk baskıyı eski/korumalı bir cihazda, zorlamadan deneyin. Çene sıkıysa modeli
  takmayın; CAD'deki açıklığı büyütün.
- Ön köşede, trackpad ve klavye alanından uzakta kullanın.
- MacBook'u çantaya koymadan önce parçayı çıkarın.
- Ekranla temas eden yüzeye ince silikon/TPU kaplama eklemek çizilme riskini azaltır.

## Dosyalar

- `flowly-fit-clip.stl`: doğrudan dilimlemeye hazır ağ
- `flowly-fit-clip.scad`: düzenlenebilir OpenSCAD modeli
- `generate_flowly_macbook_clamp.py`: logoyu SVG'den alan tekrarlanabilir STL üreticisi

Yeniden üretmek için:

```bash
python3 generate_flowly_macbook_clamp.py --voxel 0.40
```
