# Munkaidő-allokálás és havi nyilvántartás

A modul a megadott intézményi beosztási szabályokat hajtja végre. A szerződések, a csoportbeosztások és a távollétek alapján tervezetet készít; a tényleges teljesítést HR/CEO ellenőrzi és igazolja. A generálás önmagában nem igazolja, hogy a dolgozó ledolgozta a beosztott időt.

## Keretek és időszámítás

Minden számítás egész percekkel történik. A szünet nem része a kötött időnek.

| Munkakör | Beosztandó heti idő | Ebből nevelési idő | Alap napi munka | Szünet |
| --- | ---: | ---: | ---: | ---: |
| Óvodapedagógus | 32:00 | 32:00 | 6:24 | 0:20 |
| Gyakornok óvodapedagógus | 32:00 | 26:00 | 6:24 | 0:20 |
| Pedagógiai asszisztens, dajka, óvodatitkár | 40:00 | – | 8:00 | 0:20 |
| Mt. szerinti dolgozó | 40:00 | – | 8:00 | 0:20 |

A pedagógusokra vonatkozó összesítés a **kötött időt** mutatja. A gyakornok napi 5:12 nevelési és 1:12 egyéb kötött időt kap. A 40 órás szerződés kötött időn felüli része ebből nem válik automatikusan elszámolt munkaidővé. Részmunkaidős szerződésnél a keretek a heti szerződéses óraszám / 40 arányában csökkennek.

A heti cél a megfelelő ötnapos keret egyötöde szorozva a héten ténylegesen beosztható munkanapokkal. Az ünnepnapokat és a CSUPOR Munkanapok menüjében rögzített eltéréseket is figyelembe veszi: egy négynapos pedagógushét 25:36, egy hatnapos 38:24 kötött időt jelent. A jóváhagyott és a visszavonási jóváhagyásra váró távollét nulla munkaidő; a kiesést nem osztja szét a többi napra. A még jóvá nem hagyott igény nem számít igazolt távollétnek.

Pontosan hat óra munkához nincs szünet; hat órát meghaladó munkához 20 perc jár. A napi nettó munka legfeljebb 8 óra, a kezdés és vég közötti idő legfeljebb a munkaidő plusz a szünet. A szünet időpontját is tároljuk, így az nem számít bele sem a pedagógusellátásba, sem az átfedésbe.

Példa heti 30 órás, azaz napi 6 órás részmunkaidős szerződésre: az arány 30 / 40 = 75%. Ötnapos héten a pedagógus kötött és nevelési kerete egyaránt 24:00; a gyakornok kötött kerete 24:00, nevelési kerete 19:30; a NOKS/Mt. dolgozó kerete 30:00. A napi beosztott idő pedagógusnál és gyakornoknál 4:48, a gyakornok nevelési részideje 3:54, NOKS/Mt. dolgozónál 6:00. Ezekhez az időtartamokhoz nincs munkaközi szünet. A rövidebb vagy hosszabb hét és a távollét ezekre a már arányosított keretekre hat.

## Beosztási eljárás

1. A hónap határán átnyúló hetekkel együtt felépül a munkanaptár. Minden nap a szerződés és a csoporthozzárendelés aznap érvényes állapota számít. Átfedő szerződések vagy hiányzó hozzárendelések nem eredményezhetnek ugyanannak a személynek kettős beosztást.
2. A csoportok pedagógusainak alapműszakját a rögzített váltási sorrend és a referenciahétfőtől eltelt hetek határozzák meg. Ez december–január között is tényleges heti váltást ad. Hozzárendelt műszak nélkül az aznapi ellátási igény dönti el a délelőtti vagy délutáni beosztást.
3. A távolléteket a motor először kiveszi a beosztható állományból. Egy hiányzó pedagógus mellett a jelenlévő délelőttös, a délutáni pótlásra a saját csoport dajkája vagy helyi pedagógiai asszisztens választható. Egy helyettes egyidejűleg csak egy csoportot fedhet le.
4. A motor a napi műszakok rögzítése után, kizárólag az aznapi délelőttösök közül választja ki a 6 órás dajkanyitót és a 7 órás pedagógusnyitót. A nyitás kedvéért nem változtat délutános műszakot délelőttösre. A távollévő társa miatt kötelezően délelőttre kerülő pedagógus nyithat; a délutáni helyettesítésre beosztott dajka nem. A teljesíthető kiosztások között a korábbi nyitások száma alapján törekszik az egyenletes elosztásra.
5. A többi délelőttös 8 órakor kezd, a délutános 17:30-kor végez. Alaphelyzetben minden beosztható napra azonos munkaidő jut. A motor ezután a tényleges, szünet nélküli időszakokkal ellenőrzi a 8–12 közötti pedagógusellátást és a pedagóguspár legalább kétórás átfedését.
6. Két hiányzó pedagógusnál a program nem választ automatikusan befogadó csoportot. Dátumhoz kötött figyelmeztetést ad. HR/CEO választhat másik, azonos telephelyen működő csoportot; az összevonás mind az érintett nyilvántartásokban, mind az exportban megjelenik.
7. A megoldhatatlan korlátokat konkrét naphoz, csoporthoz vagy dolgozóhoz kapcsolódó hibaként jelzi. A hibát nem oldja meg túl hosszú munkanappal vagy a távollévő beosztásával. A kézi javítások ugyanazon ellenőrzésen mennek át.

A napi nyitóváltás az alkalmas délelőttös dolgozók között valósul meg. A rendszer munkavállalónként, munkavégzési helyenként és munkakörönként számolja az összes korábbi rögzített nyitást; szerződésváltáskor az előzmény nem nullázódik. Először az alacsonyabb alkalomszámot részesíti előnyben, azonosságnál kerüli az egymás utáni nyitásokat. A hónaphatár előtti napoknál a mentett nyitások számítanak, a teljes hét kiszámításához szükséges próbanapok nem növelik az alkalomszámot.

Egyetlen jelenlévő, alkalmas nyitó esetén a másnapi váltás nem teljesíthető; az ismétlődést a rendszer jelzi. Ha nincs alkalmas délelőttös nyitó, a program feloldandó hibát jelez. A heti alapműszakváltást a kifejezetten előírt távolléti helyettesítés felülírhatja; az ilyen napi eltérést a program külön jelzi. Az átfedési ellenőrzés a szünet nélküli munkavégzési jelenlétre vonatkozik; a gyakornok nevelési részkerete külön mutató.

## HR/CEO munkafolyamat

Az alábbi szerepkörök az alapértelmezett hozzáféréseket írják le. A fejlesztő az Oldalhozzáférések oldalon külön állíthatja a Munkaidő-nyilvántartás, a Munkaidő-kezelés és a Csoportok hozzáférését. A személyes oldal továbbra is csak a saját adatokat mutatja, munkaszerződés szükséges hozzá. Mások beosztásához és exportjaihoz Munkaidő-kezelés jogosultság kell; a Csoportok hozzáférése ezt önmagában nem adja meg.

A **Kezelés → Csoportok** önálló menüpont a `/groups` oldalra vezet. Felül a munkavégzési hely választható ki, alatta táblázat sorolja fel a csoportokat és a hozzájuk rendelt munkavállalókat, a hozzárendelések dátumaival. A táblázat blokkja tartalmazza a **Csoport hozzáadása** gombot; minden sorban **Szerkesztés** gomb található. A létrehozás a `/groups/new`, a szerkesztés a `/groups/<id>/edit` aloldalon történik. A Munkaidő-kezelés oldalról továbbra is elérhető a csoportlista. A régi `/worktime/groups` cím átirányít az új oldalra.

A szerkesztőoldalon a csoport neve és érvényességi időszaka, valamint a munkavállalók dátumozott hozzárendelése kezelhető. Az érvényességi időszakok megtartják a korábbi beosztások adatait; a szerződésen vagy a csoport működési idején kívüli, illetve átfedő hozzárendelést a program elutasítja. Hibás mentéskor az űrlap kitöltött értékei megmaradnak.

A műszakhoz a két heti váltási sorrend mellett **Nincs hozzárendelt műszak** választható, ez az új hozzárendelés alapértelmezése. Ilyenkor a dolgozó igény szerint délelőttös vagy délutános lehet. A rögzített műszakú pedagógus mellé a műszak nélküli társa az ellenkező műszakot kapja; két műszak nélküli pedagógus esetén a program biztosítja a délelőtti és délutáni beosztást. Egy hiányzó pedagógus mellett a csoport műszak nélküli dajkája délutánra kerül, ha az ellátási feltételek így teljesíthetők. A délutáni helyettes aznap nem nyithat is; ha emiatt nincs alkalmas nyitó, a rendszer külön hibát jelez. Az asszisztens telephelyi helyettesként csoporthozzárendelés nélkül is használható.

A HR/CEO a Munkaidő-kezelés menüben kiválasztja a telephelyet és a hónapot, majd a Munkavállaló beosztása mezőben megadhatja, kinek a nyilvántartását szeretné látni. A lista a kiválasztott helyszínen az adott hónapban szerződéssel rendelkező dolgozókat tartalmazza. A HR/CEO elkészíti a tervezetet, feloldja a hiányjelzéseket, és szükség esetén javítja a napi adatokat. A csoportösszevonás manuális döntés. A véglegesítés külön ellenőrző művelet, és hibás vagy elavult adatokkal nem engedélyezett. A kézi adatok felülírását az újragenerálásnál külön jelezni kell.

A szerződés, csoporthozzárendelés, összevonás, munkanaptár vagy távollét későbbi változása elavulttá teszi a kapcsolódó tervezetet. Ilyenkor frissítés szükséges, mielőtt újra véglegesíthető vagy exportálható. Egy korábbi böngészőablakból érkező mentés nem írhatja felül észrevétlenül az újabb változatot.

A felső szintű Munkaidő-nyilvántartás menüpont minden szerződéssel rendelkező munkavállalónak a saját beosztását és letöltéseit mutatja, HR/CEO szerepkörben is. A korábban lezárt szerződések nyilvántartása továbbra is megtekinthető a megfelelő hónap kiválasztásával. A saját oldalon nincs dolgozóválasztó, generálás, igazolás vagy csoportkezelés; mások adatai és a kezelési műveletek kizárólag a HR/CEO Munkaidő-kezelés oldalán érhetők el.

## Havi export

A PDF álló, egyoldalas A4-es nyilvántartás, a CSV táblázatkezelőben feldolgozható. A dolgozó neve, az év/hónap, a munkakör, a munkavégzési hely és a csoport a fejlécben szerepel. Több munkakör, helyszín vagy csoport esetén a hozzárendelés napjai is azonosíthatók. A napi táblázat a kezdést és befejezést, a nettó munkaidőt, a szünetet és az aláíráshelyet tartalmazza; a heti és havi összesítések is megmaradnak. A távollét nulla órával, az összevonás jelöléssel szerepel. A tervezet állapota látható marad az exporton is.

A PDF jelmagyarázata feloldja a távollétek és megjegyzések rövid kódjait. A helyigényes szövegek rövidítését külön jelöli; a teljes fejléc, minden hozzárendelés és megjegyzés a CSV-ben megmarad. Ha egy nap több idősávot tartalmaz, a PDF ezt külön jelzi, és csak a tényleges perceket összesíti; a CSV minden idősávot külön sorban tart meg.

A hónaphatáron átnyúló hétnél a havi nyilvántartás csak az adott hónapba eső napokat összegzi, és ezt külön jelöli. Nem számítja másodszor a szomszédos havi nyilvántartás napjait.

## Üzemeltetés

Az öt új tábla (`work_groups`, `work_assignments`, `work_group_merges`, `work_schedules`, `work_time_entries`) induláskor létrejön. A már létező MySQL azonosítók méretéhez és előjelességéhez az új idegen kulcsok alkalmazkodnak. A PDF-export a rögzített ReportLab-verziót és a csomagolt, magyar karaktereket támogató betűkészletet használja.

A munkavállaló a Munkaidő-kezelés oldal alján, a Munkaidő-beosztás doboz tetején választható ki. A választás megtartja az évet, a hónapot és a munkavégzési helyet; a felső időszakszűrő a kiválasztott dolgozót is megőrzi, amennyiben az új időszakban és helyszínen rendelkezik szerződéssel. A táblázatokban, valamint a PDF- és CSV-exportban az óraszám megnevezése **Kötött idő**.


A műszak nélküli hozzárendeléshez a `work_assignments.flexible_shift` logikai mező került be. Alapértéke hamis, ezért a meglévő 0/1 műszakok megmaradnak. Az alkalmazás induláskor hozzáadja a hiányzó oszlopot; ehhez `ALTER` jogosultság szükséges. Ugyanez külön, megismételhető SQL-migrációként is elérhető: `sql/migrations/2026-10-04-add-flexible-work-assignment-shifts.sql`. A migráció nem töröl táblát, oszlopot vagy hozzárendelést, és ismételt futtatáskor nem állítja vissza a mentett beállításokat.

A beosztási szabályverzió 3-ra változott. A korábban generált havi nyilvántartásokat az új szabályok szerinti igazolás/export előtt újra kell generálni. A mentett bejegyzéseket az indulás nem írja át; a kézi bejegyzések felülírásához továbbra is külön választás szükséges.
