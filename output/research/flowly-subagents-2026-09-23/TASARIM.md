# Flowly: istek üzerine oluşturulan subagent sistemi

23 Eylül 2026 — araştırma ve uygulanacak tasarım; ürün kodunda değişiklik yapılmadı.

Core: `c74a81042985927f27c41a0f8e7f86d192a70f58`. Desktop: `0ddea2fac817c6e499c8c436f44527a24fa2e86b` ve mevcut çalışma ağacı. Başka makinedeki olayın kayıtlarına erişilmedi; aşağıdaki bulgular güncel kod ve izole denemelerden geliyor.

## Karar

Flowly işi kendisi yapar; kullanıcı açıkça delegasyon istediğinde o görev için bir subagent oluşturur, çalışırken onunla haberleşir ve sonucunu teslim eder. Kullanıcı önce bir researcher/coder/persona tanımlamaz. Model seçimi görev talebinin parçasıdır: “Composer hatasını Luna'ya ver.”

“Geçici agent”, görev için oluşturulan çalışma bağlamıdır; sonuçların veya dosyaların geçici olması anlamına gelmez.

Bu tasarım subagent oluşturma, yönetme, mesajlaşma ve teslim zincirini kapsar. SOUL, normal bot profilleri, genel persona sistemi ve önceki 17 maddelik listenin kalan bağımsız işleri kapsam dışındadır. Desktop içinde ayrıntılı canlı sohbet görünümü sonraki aşamadır; gerekli protokol bu aşamada hazırlanır.

## Güncel kodda doğrulanan farklar

| Bulgu | Kanıt | Tasarıma etkisi |
| --- | --- | --- |
| Süreye göre delegasyon yönlendirmesi hâlâ var. | [context.py:2047](/Users/hakanoren/flowly-repos/flowly/flowly/agent/context.py:2047), 15 saniyeyi aşan işleri `spawn` ile çalıştırmasını söylüyor. | Süreye ve iş türüne göre otomatik delegasyon kaldırılmalı. |
| Eski uzman yönlendirmesi hâlâ etkin. | [context.py:1615](/Users/hakanoren/flowly-repos/flowly/flowly/agent/context.py:1615), araştırma/yazma işlerinde researcher seçmesini ve sonucuna güvenmesini söylüyor. | Kısa bir görev sahipliği ilkesiyle değişmeli; yalnızca UI kaldırmak yeterli değil. |
| İstenen model kaybolabiliyor. | [loop.py:6780](/Users/hakanoren/flowly-repos/flowly/flowly/agent/loop.py:6780), `spawn` argümanlarını anahtar kelimeyle `{agent, task}` olarak yeniden kuruyor. | Model, etiket ve süre gibi bilgiler yolda silinmemeli; bu yönlendirme tamamen kalkmalı. |
| Ana agent zorla işi bırakıyor. | [subagent.py:632](/Users/hakanoren/flowly-repos/flowly/flowly/agent/subagent.py:632) yanıtı turu bitirmesini söylüyor; [loop.py:4817](/Users/hakanoren/flowly-repos/flowly/flowly/agent/loop.py:4817) async uzman çağrısından sonra bütün araçları gizliyor. | Ana agent bağımsız işini sürdürebilmeli, gerektiğinde bekleyebilmeli ve mesaj gönderebilmeli. |
| Sonuç kalıcılığı artık kısmen mevcut. | 14 Eylül tarihli `90cb3937` değişikliği; [subagent_registry.py:227](/Users/hakanoren/flowly-repos/flowly/flowly/agent/subagent_registry.py:227) tam sonucu saklıyor; `subagents.get/result`, sürümlü olaylar ve artifact kimlik alanı var. | “Sonuç hiç saklanmıyor” eski bulgusunu tekrarlamamalıyız; mevcut sözleşmeyi geliştireceğiz. |
| Saklama süresi uzun vadeli değil. | [subagent_registry.py:132](/Users/hakanoren/flowly-repos/flowly/flowly/agent/subagent_registry.py:132), bitmiş ve duyurulmuş/duyuru gerektirmeyen kayıtları 24 saat sonra eliyor; ilgili sonuç dosyaları yazım sırasında temizlenebiliyor. | Kullanıcı görevlerinin geçmişi 24 saatlik çalışma kaydıyla aynı ömre sahip olmamalı. |
| Tam sonuca ulaşma yolu ana modele eksik aktarılıyor. | [subagent.py:1508](/Users/hakanoren/flowly-repos/flowly/flowly/agent/subagent.py:1508) duyuruyu 2.000 karaktere kesiyor; [sessions_list.py:58](/Users/hakanoren/flowly-repos/flowly/flowly/agent/tools/sessions_list.py:58) yalnızca list/cancel sunuyor. | Tam sonuç için model aracı ve kimlik içeren yapılandırılmış teslim gerekli. |
| Çift yönlü subagent mesaj kutusu yok. | `SubagentManager` başlangıç, çalışma, bitiş duyurusu ve iptal sunuyor; çalışan child'a mesaj ekleyen araç/kalıcı inbox yok. | Başlangıç promptuna yeni bir alan eklemek yetmez; çalışma döngüsü mesaj tüketmeli. |
| Profilde oluşturulan artifact ortak kütüphaneyi atlayabiliyor. | [subagent.py:713](/Users/hakanoren/flowly-repos/flowly/flowly/agent/subagent.py:713) ve [subagent.py:1669](/Users/hakanoren/flowly-repos/flowly/flowly/agent/subagent.py:1669) doğrudan profil deposunu kullanıyor; ana profil aracı [shared_service.py:54](/Users/hakanoren/flowly-repos/flowly/flowly/agent/tools/shared_service.py:54) ortak servise yönlendiriyor. | Child ve ana agent aynı artifact hizmetinden geçmeli. |
| Başarılı artifact oluşturma görev kaydına bağlanmayabiliyor. | [subagent.py:1062](/Users/hakanoren/flowly-repos/flowly/flowly/agent/subagent.py:1062) üst düzey `id` okuyor; [artifact.py:256](/Users/hakanoren/flowly-repos/flowly/flowly/agent/tools/artifact.py:256) kimliği `artifact.id` altında döndürüyor. | Yapılandırılmış oluşturma makbuzu kullanılmalı. |
| Artifact'i listelemek bile otomatik kaydı engelleyebiliyor. | [subagent.py:1012](/Users/hakanoren/flowly-repos/flowly/flowly/agent/subagent.py:1012), bütün artifact işlemlerinde aynı bayrağı işaretliyor. | Okuma, başarısız oluşturma ve doğrulanmış kaydetme birbirinden ayrılmalı. |
| Goal bekleme mekanizması subagent bağımlılığını doğrudan izlemiyor. | [loop.py:1301](/Users/hakanoren/flowly-repos/flowly/flowly/agent/loop.py:1301) plan/process beklemesi; [loop.py:1330](/Users/hakanoren/flowly-repos/flowly/flowly/agent/loop.py:1330) yalnızca process listesini bağlıyor. | Goal ile çocuk görev ilişkisi açıkça kaydedilmeli. |
| Eski oluşturma ekranı gerçekten kullanılıyor. | [AgentsTab.tsx:491](/Users/hakanoren/flowly-desktop/src/renderer/src/pages/Dashboard/AgentsTab.tsx:491), [AssistantsSection.tsx:39](/Users/hakanoren/flowly-desktop/src/renderer/src/pages/Dashboard/AssistantsSection.tsx:39). | Ekranla birlikte arkasındaki yazma/model ayarlama yüzeyleri de emekliye ayrılmalı. |

Yetkiler tamamen yok değil: [tool_context.py](/Users/hakanoren/flowly-repos/flowly/flowly/agent/tool_context.py) ve tool registry çağıran oturumun araç tavanını denetliyor; child tarafında ayrıca bir engel listesi var. Eksik olan, yeni görev kimliğiyle bu sınırın bütün girişlerde tutarlı taşınması ve ana agent'ın uygun araçlarının güvenli biçimde child'a sunulmasıdır.

## İzole denemelerin sonucu

Gerçek model/API kullanılmadı; sahte sağlayıcılar ve geçici depolar kullanıldı. Çalıştırılabilir [probe.py](/Users/hakanoren/flowly-repos/flowly/output/research/flowly-subagents-2026-09-23/probe.py) ve [ham sonuçlar](/Users/hakanoren/flowly-repos/flowly/output/research/flowly-subagents-2026-09-23/probe-results.json) aynı klasörde.

| Deneme | Gözlenen sonuç |
| --- | --- |
| `spawn(task="Research the Composer issue", model="gpt-6-luna", ...)` yönlendirme bloğu | Gerçek kaynak AST'si çalıştırıldığında çağrı `builtin_agent(researcher, task)` oldu; model/etiket/süre silindi. |
| 3.000 karakterlik normal child sonucu | Tam metin saklandı; ana modele giden duyuruda run kimliği yoktu. |
| Önce artifact listesi, sonra otomatik kaydı açık 3.000 karakterlik sonuç | Artifact sayısı sıfır kaldı; tam görev sonucu ayrıca saklandı. |
| Child'ın doğrudan `artifact.create` kullanması | Depoda bir artifact vardı; görev kaydının `artifact_ids` alanı boş kaldı. |
| Profil child'ında 7.000 karakterlik uzun sonuç | Profilde bir artifact vardı; ortak kütüphane listesi boştu; kimliği zaten biliniyorsa yerel get çalışıyordu. |

198 mevcut test geçti: subagent observation, event compatibility, profile transport, incremental delivery, safety, assistants, shared service ve goal/process wait dosyaları. Tek uyarı mevcut Pydantic deprecation uyarısıydı. Bu testler yeni tasarımı doğrulamıyor: örneğin `test_policy_hides_all_tools_after_async_dispatch`, kaldıracağımız davranışı bugün başarı ölçütü sayıyor. Başka makinedeki olaya birebir kök neden atamıyoruz.

## Codex ile doğrulanan karşılaştırma

Güncel resmi doküman, yerel Codex'te doğrudan kullanıcı isteği veya geçerli proje/skill talimatıyla delegasyon yapıldığını; model belirtilebildiğini, belirtilmezse parent ayarlarının miras alınabildiğini ve devam talimatı/bekleme/kapatma işlemlerini açıklıyor. Önceden özel agent tanımlamak zorunlu değil; isteğe bağlı özel agent dosyaları da destekleniyor. Flowly için burada daha dar bir ürün kararı alıyoruz: sohbet delegasyonunda açık kullanıcı isteği gerekli. [Resmi subagent dokümanı](https://learn.chatgpt.com/docs/agent-configuration/subagents)

Bu oturumda sunulan araçlar da görev metniyle `spawn_agent`, çalışan agent'a `send_message`, devam görevi, bekleme ve kesme işlemlerini ayrı tutuyor. Buradan Flowly için çıkardığımız tasarım, görev oluşturma ile çalışırken yönlendirmeyi ayrı operasyonlar yapmak.

OpenAI'nin ayrı Agents API dokümanı oluşturma, mesaj, bekleme ve kesme olaylarını; olay akışı ile saklanan geçmişin farklı olduğunu belgeliyor. Bunu teslim protokolü için referans alıyoruz; API ile masaüstü Codex'in iç uygulamasının aynı olduğunu varsaymıyoruz ve Flowly'yi bu API'ye taşımayı önermiyoruz. [Multi-agent API dokümanı](https://developers.openai.com/api/docs/guides/agents-api/multi-agent)

## Kullanıcıdan beklenen davranış

| Kullanıcı talebi | Beklenen davranış |
| --- | --- |
| “Bunu araştır.” / “Bu hatayı düzelt.” / “Bir goal belirledim.” | Ana Flowly çalışır; subagent oluşturulmaz. |
| “Bu hatayı Luna agent'a ver.” | Flowly görev brief'ini çıkarır, Luna erişimini doğrular ve tek child oluşturur. |
| “İki agent paralel incelesin.” | İşi iki bağımsız kapsama ayırır; model belirtilmemişse o anki parent modelini kullanır. |
| “Luna'ya sadece Composer dosyalarına baksın de.” | Yeni agent açmadan mevcut child'a yönlendirme iletir. |
| “Araştırmayı ona ver, sen testlere bak.” | Child araştırır; parent testleri yürütür. |
| “Subagent kullanma, kendin devam et.” | Yeni delegasyonu kapatır, ilgili child işini güvenli sınırda durdurur ve eldeki sonuçları parent'a aktarır. |
| “Sonucu kaydet/aç.” | Mevcut sonuç veya artifact üzerinden işlem yapar; araştırmayı tekrar başlatmaz. |
| Talep edilen model erişilebilir değil | Başka modele sessizce geçmez; modelin kullanılamadığını açıkça bildirir. |

## Çalışma tasarımı

### 1. Delegasyon yetkisi ve görev sahipliği

Süre eşiği, researcher/coder kelime eşlemesi, otomatik uzman seçimi ve “uzmana güven, turu bitir” yönlendirmeleri kaldırılacak. Ana yönerge kısa olacak: işi sahiplen; kullanıcı delegasyon isterse kapsamı belirli bir görev oluştur; sonucu doğrula ve teslim et.

Sadece prompt değişikliğiyle yetinilmeyecek. Oluşturma servisi, gerçek kullanıcı mesajına bağlı `DelegationGrant` isteyecek: kaynak mesaj kimliği, parent oturumu, izin verilen görev kapsamı, model tercihi, child sayısı sınırı ve iptal durumu. Modelin tool argümanına `authorized=true` yazması izin oluşturmayacak.

Doğal dilde “açık istek” anlamaya ilişkin öneri: yalnızca yeni bir delegasyon önerildiğinde, geçerli kapsam izni yoksa, seçili ana modelle dar kapsamlı bir niyet doğrulaması yapılacak; gerçek kullanıcı mesajı ve gerekli yakın bağlam üzerinden `allowed/denied/ambiguous` sonucu ve kaynak metin aralığı üretilecek. Araç/child çıktıları izin kaynağı sayılmayacak. Runtime kaynak kimliğini, kapsamı, model tercihini ve kotayı denetleyip izni saklayacak. Normal sohbet/goal turlarına ek sınıflandırma çağrısı konmayacak. Belirsiz talepte subagent açılmayacak; ana Flowly işi sürdürebilecek.

Bu seçim, açıkça istenen ilk delegasyona bir doğrulama gecikmesi ekler; varsayılan ana-agent akışına eklemez. Amaç yeni bir anahtar kelime listesi değil, doğal dil kararını ayrı bir yetki sınırına bağlamak. Semantik karar hâlâ model kararıdır; yüzde yüz doğruluk iddiası yerine olumsuzlama, alıntı, örnek anlatımı, geçmiş talep ve kapsam değişikliği testleriyle ölçülecek.

Kullanıcının tek göreve verdiği izin sonraki ilgisiz işlere taşınmayacak. Aynı izin kapsamındaki yönlendirme ve devam çalışması için yeniden kullanıcı onayı istenmeyecek. Yeni kullanıcı düzeltmesi izni daraltabilecek veya kaldırabilecek.

### 2. Şablonsuz görev ve gerçek model seçimi

`spawn` tek oluşturma girişi olacak; `builtin_agent` modeli seçen/yönlendiren katman olmaktan çıkacak. Flowly kısa ama yeterli bir brief hazırlayacak: amaç, ilgili dosyalar/kaynaklar, kapsam dışı işler, kabul ölçütü, beklenen çıktı ve parent'a bildirilmesi gereken engeller.

Runtime, modelden bağımsız olarak owner/profile/parent/session kimliklerini, çalışma dizinini, izin tavanını ve artifact hizmetini bağlayacak. Mutable `set_context` alanları görev sahipliğinin kaynağı olmayacak; mevcut `ToolCallOrigin` üzerinden değişmez bir görev bağlamı üretilecek.

Model belirtilmemişse global manager varsayılanı yerine **o turun etkili modeli ve reasoning ayarı** miras alınacak. Belirtilmişse model kataloğu ve mevcut sağlayıcı yapılandırmasıyla çözülecek; gerekiyorsa [providers/factory.py](/Users/hakanoren/flowly-repos/flowly/flowly/providers/factory.py) üzerinden doğru sağlayıcı kurulacak. Aynı provider nesnesine her model adını göndermek yeterli kabul edilmeyecek. Başka model seçildiğinde reasoning belirtilmemişse o modelin desteklenen varsayılanı kullanılacak. İstenen ve gerçekleşen model/sağlayıcı kayda yazılacak.

Child, görev için gerekli parent araçlarının izinli alt kümesini alacak; MCP keşfi sonradan gelse de izin tavanı genişlemeyecek. Yetki kontrolü gerçek dispatch sırasında da çalışacak. Modelin belirttiği dosya kapsamı sandbox yerine geçmeyecek. Paylaşılan dosyalara yazan işler parent tarafından ayrılacak; aynı dosyaya eşzamanlı yazma gerektiren işler sıraya alınacak. İlk sürümde child'ın yeni child oluşturması kapalı kalacak.

### 3. Kalıcı agent bağlamı, çalışma kaydı ve mesaj kutusu

Bir görev agent'ının kimliği (`agent_id`) ile her çalışma/yeniden devam denemesi (`run_id`) ayrılacak. Böylece “Luna'ya bunu da söyle” yeni bir şablon veya sıfırdan başka agent gerektirmeyecek.

Mevcut `SubagentRegistry` çağrı yüzeyi ve v2 görüntü sözleşmesi korunarak altında SQLite tabanlı bir görev deposuna geçilecek. Neden: yeni mesajın kabulü, durum geçişi ve teslim kuyruğunun aynı transaction içinde saklanabilmesi. Mevcut atomik JSON sonuç altyapısı başarısız sayılmıyor; çift yönlü çalışma yeni ilişkiler ve transaction ihtiyacı getiriyor.

Önerilen tablolar: `delegation_grants`, `agent_threads`, `runs`, `messages`, `artifact_refs`, `outbox`. Her kayıtta profile/root-session/parent ilişkisi bulunacak. Büyük sonuçlar ayrı dosyada kalabilecek: önce atomik dosya yazımı, ardından referans transaction'ı; referanssız kalan dosyalar sonradan temizlenebilecek. Eski run kimlikleri ve sonuç dosyaları göçte korunacak.

Çalışma durumları: `queued`, `running`, `waiting_for_parent`, `waiting_for_user`, `completed`, `failed`, `cancelled`, `interrupted`. Agent bağlamı tamamlanmış bir çalışmadan sonra devam talimatı için saklanabilecek; kapatılması geçmişini silmeyecek. Başarılı çalışma, sonucun kullanıcıya teslim edildiği anlamına gelmeyecek; teslim durumu ayrı tutulacak.

Mesaj alanları: `message_id`, gönderici/alıcı kimliği, `run_id`, sıra numarası, tür, içerik, `reply_to`, oluşturma ve teslim bilgisi. Gönderici kimliği runtime tarafından doldurulacak. Mesaj diske yazılmadan “kabul edildi” dönülmeyecek; yeniden deneme aynı idempotency anahtarıyla ikinci mesaj üretmeyecek.

| Araç | Sözleşme |
| --- | --- |
| `spawn` | Görev brief'i ve isteğe bağlı model/reasoning alır; tam agent/run kimliği, gerçek model ve başlangıç durumu döndürür. |
| `agent_message` | Çalışan veya cevap bekleyen parent/child'a mesaj, soru veya cevap yollar; kabul makbuzu döndürür. |
| `agent_wait` | Seçili agent'ları cursor ile bekler; yeni sonuç, soru, hata veya kullanıcı girdisinde kontrolü bırakır. |
| `agent_read` | İzin verilen görevin durumunu, mesajlarını veya tam sonucunu sayfalı okur. |
| `agent_continue` | Saklanan child bağlamında yeni bir çalışma başlatır; eski çalışmayı başarı/başarısızlık açısından yeniden yazmaz. |
| `agent_interrupt` | İlgili child'ı durdurur; bütün sohbetleri veya kardeş görevleri varsayılan olarak iptal etmez. |

Mesajlaşma parent↔child arasında iki yönlü olacak. Aynı görev ağacındaki kardeşlerin haberleşmesi parent'ın verdiği peer kapsamıyla mümkün olacak; ilgisiz sohbetlere/profile'lara mesaj gönderilemeyecek. Bitmiş veya kapatılmış hedefe normal mesaj sessizce kaybolmayacak; durum ve gerekiyorsa `agent_continue` gerekliliği dönecek.

### 4. Çalışırken yönlendirme ve ana döngü

Child mesajları bir sonraki güvenli model/araç sınırında okuyacak. Uzun süren araç çağrısı sırf yeni mesaj geldi diye tekrar çalıştırılmayacak. Acil kesme ayrı işlem olacak. Parent da çalışırken gelen child sorusunu aynı yöntemle alabilecek; parent boşta ise scheduler bir devam turu başlatacak.

Mevcut [run_steering.py](/Users/hakanoren/flowly-repos/flowly/flowly/agent/run_steering.py) kalıcılıktan sonra müdahale etme ve araç çağrısını yanlışlıkla kesmeme bakımından kullanılabilir bir temel. Ancak bugün mesajları kullanıcı rolüyle ekliyor ve aktif run için tasarlanmış; child mesajları bu metoda doğrudan kullanıcı girdisi gibi sokulmayacak. Ortak sınır/uyandırma altyapısı çıkarılacak, agent mesajının güven düzeyi korunacak.

Ana agent'ın araçlarını topluca gizleme ve her spawn sonrasında zorla final acknowledgement üretme kalkacak. Parent bağımsız işi varsa devam edecek; yalnızca child sonucuna bağımlıysa `agent_wait` ile kontrolü bırakacak. Child'ın normal ilerleme olayları UI'a gidecek; her heartbeat yeni parent LLM çağrısı doğurmayacak. Sorular ve bitişler gerekli devamı tetikleyecek; tek parent oturumunda aynı anda iki model turu çalışmayacak.

Goal, kendisine bağlı gerekli child sonuçlarını bekleyecek; “görev kabul edildi” veya “parent turu bitti” durumunu tamamlanma saymayacak. Çocuk hata verirse hata ve eldeki çıktılar parent'a gelecek; başarısızlık sonsuz yeniden spawn döngüsüne dönüşmeyecek.

### 5. Sonuç ve artifact teslimi

Her çalışma, kısa olsun uzun olsun, tam sonuç kaydı bırakacak. Modele kısa özet verilebilir ama yanında daima run kimliği, sonuç okuma referansı ve doğrulanmış artifact referansları bulunacak. `agent_read` tam içeriği getirebilecek; parent yalnızca 2.000 karakterlik duyuruya mahkûm olmayacak.

Kullanıcıya yönelik artifact üretimi parent ve child için aynı ortak hizmete bağlanacak. Yerel büyük bağlam artıkları ayrı kalabilecek. “Kaydedildi” ifadesinin dayanağı tool'a dokunulması değil, başarılı kalıcı kaydın `artifact_id`, sürüm, owner ve açma referansı içeren makbuzu olacak. Listeleme, okuma ve başarısız create bu makbuzu üretmeyecek.

İş sonucu ile kullanıcı teslimatı ayrılacak: örneğin rapor tamamlanmış fakat ortak kütüphane erişilemiyorsa rapor korunacak, teslimat bekliyor/hatalı gösterilecek ve aynı idempotency anahtarıyla yayınlama yeniden denenecek. Sessizce başka depoya yazıp başarı bildirilmeyecek. Ayrı görev ve artifact depoları arasında atomiklik varsayılmayacak; outbox ve idempotent yayınlama kullanılacak.

24 saatlik budama kullanıcı görevleri için kaldırılacak; görev geçmişi bağlı konuşmanın saklama/silme politikasıyla yaşayacak. Kullanıcının kütüphaneye kaydettiği artifact'in ömrü görevin kapanmasından bağımsız olacak. İç bakım işleri için kısa süreli saklama ayrı politika olabilir.

Eski görünmeyen artifact'ler için yeniden üretim yapılmayacak: profil depolarında yalnızca kullanıcıya yönelik kayıtları listeleyen dry-run envanteri, kaynak kimliği eşlemesi, içerik doğrulaması ve tekrar çalıştırıldığında çoğaltmayan bir aktarım hazırlanacak. Kimlik çakışmıyorsa eski ID korunacak; çakışıyorsa kaynak-profile+eski-ID alias'ı saklanacak. Kaynak kayıtlar başlangıçta silinmeyecek; internal context artıkları topluca kullanıcının kütüphanesine taşınmayacak.

### 6. Eski yüzeyleri kaldırma ve istemci sözleşmesi

Desktop'taki “Your agents / Create agent” ve uzman başına kalıcı model ayarlama kaldırılacak. Core'da `assistants.write/delete`, uzman şablonu yükleme ve ilgili TUI yapılandırma komutları yeni mimaride delegasyon yolu olmayacak. Eski dosyalar veri kaybı yaratmadan yerinde kalacak; otomatik yüklenmeyecek. Eski istemciler capability cevabıyla bu özelliğin kaldırıldığını öğrenecek; mutasyonlar açık bir `FEATURE_RETIRED` sonucu verecek.

Normal bot profili oluşturma ve dış CLI sağlayıcı bağlantıları farklı kavramlar. Bunlar bu işte silinmeyecek. Bununla birlikte `delegate_to` gibi alternatif yürütme yolları sohbet delegasyon kuralını aşamayacak. Board/cron/bakım yürütücülerinin çalışma kaynağı runtime tarafından ayrı tanımlanacak; model sıradan bir sohbet işine `origin=maintenance` diyerek izin atlayamayacak. Goal oluşturmak delegasyon izni üretmeyecek.

Mevcut `subagents.list/get/result` ve eski started/completed olayları korunacak. Yeni istemci capability ile messages/continue/wait ve ayrıntılı olay desteğini öğrenecek. Local gateway, relay ve profile-host geçişleri aynı sözleşmeyi kullanacak; yalnızca yerel IPC'ye eklemek yeterli olmayacak. [profile_host_contract.py:43](/Users/hakanoren/flowly-repos/flowly/flowly/profile_host_contract.py:43) bugün sadece alt küme sunuyor.

Yeni olaylarda tam `agent_id`, `run_id`, `parent_session_key`, `revision/sequence`, olay türü ve artifact referansları olacak. Durum/ilerleme güncellemeleri birleştirilebilecek; soru, mesaj ve sonuç teslimi best-effort olay kuyruğuna bırakılmayacak. Yeniden bağlanmada snapshot+cursor ile eksikler okunacak; UI event almakla tam geçmişin sahibi olmayacak. Modelin gizli düşünce süreci değil, görünür ilerleme metni, araç durumu, soru ve sonuç yayınlanacak.

Sonraki Desktop aşamasında bu veriler sohbet içi bir görev kartına dönüşecek: gerçek model, çalışma durumu, son görünür ilerleme, soru, sonucu aç ve durdur. Mevcut [useSubagents.ts](/Users/hakanoren/flowly-desktop/src/renderer/src/hooks/useSubagents.ts) polling tabanlı; mevcut liste/pet etkinliği, karşılıklı agent sohbetiyle aynı özellik değil.

## Uygulama sırası

Her maddenin altındaki cümle yapılacak işi özetler; bu sıra önerilen PR sınırlarıdır.

1. **Davranış sözleşmesi ve delegasyon girişi**
   Açık kullanıcı isteğini görev kapsamına bağlayan izin mekanizmasını ekleyip süre/kelime/uzman yönlendirmelerini kaldıracağız.

2. **Göreve özel agent, model ve yetki çözümü**
   Parent'ın o anki bağlamından değişmez görev brief'i oluşturup istenen modeli doğru sağlayıcı ve devralınan izinlerle çalıştıracağız.

3. **Kalıcı görev bağlamı ve çift yönlü mesajlaşma**
   Mevcut kayıt sözleşmesini koruyan görev deposuna mesaj kutusu, devam çalışması, idempotency ve parent/child erişim denetimi ekleyeceğiz.

4. **Ana agent ve goal koordinasyonu**
   Zorunlu tur bitirmeyi kaldırıp gelen child mesajlarını güvenli sınırlarda işleyen, bağımlılığı bekleyen ve ana agent'ın bağımsız işini sürdüren akışı kuracağız.

5. **Tam sonuç, ortak artifact ve eski kayıtların aktarımı**
   Sonuçları okunabilir kimliklerle teslim edip child artifact'lerini ortak kütüphanede doğrulayacak ve eski kullanıcı çıktılarını çoğaltmadan taşıyacağız.

6. **Eski oluşturma ekranlarını kapatma ve tüm taşıma yolları**
   Desktop/TUI şablon oluşturmayı kaldırıp yeni yetenekleri local, relay ve profile-host üzerinde sürümlü olarak yayınlayacağız.

7. **Uçtan uca kabul ve kontrollü geçiş**
   Aşağıdaki senaryolar geçtikten sonra yeni akışı açıp eski kayıtları koruyarak hata, gecikme ve teslimat ölçümlerini izleyeceğiz.

8. **Sonraki aşama: sohbet içi canlı agent kartları**
   Hazır olay ve geçmiş sözleşmesini kullanarak agent'ın ilerlemesini, sorularını ve çıktısını Desktop sohbetinde göstereceğiz.

Bağımlılık: 1→2→3→4; 5'in ortak artifact düzeltmesi erken hazırlanabilir, görev teslim entegrasyonu 3'e dayanır; 6 yeni Core sözleşmesiyle birlikte çıkar; 7 geçmeden özellik varsayılan açılmaz; 8 ayrı görsel teslimattır. İlk uçtan uca dilim tek child ve tek Composer görevi olacak; çoklu agent görünümü bu dilimin önüne geçmeyecek.

## Kabul ölçütleri

1. “Araştır”, “goal koydum”, “Luna nedir?”, alıntılanmış spawn örneği ve “agent kullanma” vakalarında child sayısı sıfırdır.
2. Açık Luna isteğinde tam bir child açılır; gerçek model Luna'dır; sağlanamıyorsa sessiz fallback olmaz.
3. Aynı isteğin ağ üzerinden yeniden denenmesi tek agent/çalışma üretir; yeni bir araştırma sırf aynı uzman etiketiyle 10 dakika içinde geldi diye engellenmez.
4. İki eşzamanlı parent oturumunun görevleri, modelleri, mesajları ve artifact'leri birbirine karışmaz.
5. Parent çalışırken child'ın sorusu, child çalışırken parent'ın yönlendirmesi ulaşır; araç ortasında gelen mesaj yan etkili aracı tekrar yürütmez.
6. Aynı mesajın tekrar teslimi tek kez işlenir; uygulama kabul makbuzundan sonra kapanırsa mesaj kaybolmaz.
7. Child'ın bitmesi, başarısızlığı veya soru sorması bekleyen parent/goal'u uygun biçimde uyandırır; ilerleme heartbeat'leri gereksiz LLM turu üretmez.
8. Tam sonuç restart sonrasında ve 24 saatten sonra açılır; iptal edilen iş başarı gösterilmez; restart yan etkili işi otomatik tekrar çalıştırmaz.
9. Artifact create/list/error ayrımı doğrudur; child'ın artifact'i seçili botun ortak kütüphanesinde ve görev sonucunda aynı kimlikle açılır.
10. Kütüphane kesintisinde tam sonuç korunur ve yayınlama bekliyor görünür; tekrar deneme ikinci artifact üretmez.
11. Eski artifact göçü iki kez çalıştırıldığında sayı değişmez; internal spill'ler özel kalır; eski bağlantılar çözülür.
12. Local, relay ve profile-host için aynı oluşturma/mesaj/sonuç/iptal senaryoları geçer; eski istemcinin olay görüntüsü bozulmaz.
13. Child parent izinlerini genişletemez, gönderen kimliği uyduramaz ve başka sohbetin sonucunu okuyamaz.
14. İlk kullanıcıya görünür ilerleme, ilk anlamlı yanıt ve toplam tamamlanma p50/p95 süreleri; LLM çağrısı/token sayısı; mesaj teslim gecikmesi ve başarısız artifact açma oranı ölçülür.

Gecikme için bugün sayısal bir iyileşme iddia etmiyoruz: gerçek sağlayıcı ölçümü yapılmadı. Beklenen kazanç, sıradan işlerde gereksiz child başlatma ve ikinci teslim turunun kalkmasıdır; delegasyon ise yalnızca kullanıcı istediğinde ve bağımsız iş gerçekten paralel yürüdüğünde ek fayda sağlar.

## Geçiş güvenilirliği

Mevcut JSON kayıtları ve sonuçları bir kez, kimlikleri korunarak yeni depoya alınacak; geçiş öncesi yedek ve bütünlük kontrolü yapılacak. İki depoya sürekli çift yazım yapılmayacak. Eski istemciye v2 görünümü adapter ile sunulacak. Yeni çalışmalar başladıktan sonra yalnızca eski yedeğe dönmek veri kaybı yaratacağından rollback, işler durdurulup yeni kayıtlar uyumlu biçimde dışarı aktarılarak yapılacak. Göç hatası boş bir geçmişe sessiz geçişe dönüşmeyecek.

İlk geliştirme için önerilen başlangıç, PR 1 ve PR 2 ile “ana Flowly varsayılan çalışır; açık Luna isteği tek ve doğru modelle child açar” davranışını tamamlamaktır; mesajlaşma, teslim ve istemci kaldırmaları bu çekirdeği izler.
