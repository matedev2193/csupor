# Önálló tűz- és munkavédelmi oktatórendszer

A `safety/` külön Flask-alkalmazás, saját adatbázissal, PDF-tárolóval,
sütivel és jogosultságokkal. A CSUPOR személyügyi felületébe nem épül be,
nem importálja azt, és nem olvassa vagy módosítja annak adatbázisát.
A közös Micimackó-fiók szolgáltatáson keresztül lehet bejelentkezni.
A három alkalmazás forrása ugyanebben a Git-tárolóban található,
de külön folyamatként és külön címen telepítendők.

## Használat

1. Az adminfelületen válaszd az **Új tananyag** lehetőséget, add meg az évet,
   és válaszd a **Tűzvédelem** vagy **Munkavédelem** típust.
2. Tölts fel egy PDF-et. A két témához két külön tananyag tartozik.
   Legfeljebb 25 MiB-os, 500 oldalas, jelszó nélküli PDF tölthető fel.
   A szerver ellenőrzi a PDF szerkezetét és maga állapítja meg az oldalszámot.
3. Tananyagonként válassz lezárást: **Elfogadás** vagy **Vizsga**.
   A következő évben mindkét témához újra meghatározhatod ezt.
4. Vizsgánál a felületen add meg a kérdéseket, kérdésenként 2–6 választ és
   az egyetlen helyes választ. Állítsd be az elvárt százalékot is.
   Minden kérdés egy pontot ér; sikertelen vizsga újra próbálható.
5. A mentés először piszkozatot hoz létre. A **Közzététel** után lesz
   elérhető a munkatársaknak. A piszkozat nem jelenik meg a tanulói oldalon.
6. A munkatárs a PDF-olvasóban lapozhat, nagyíthat és folytathatja a
   megkezdett anyagot. Minden oldal megnyitása után lezárhatja elfogadással
   vagy vizsgával. Az elfogadás szövegét és idejét, a vizsgák válaszait,
   pontszámait és eredményét a rendszer eltárolja.
7. Az adminfelületen és a CSV-kimutatásban követhető a legutóbbi és a
   legtávolabbi oldal, a megnyitott oldalak, az aktív idő és a teljesítés.
   A névsorban előre felvett, még nem tanuló aktív munkatárs is látszik a
   közzétett anyagoknál. Az archivált anyagokhoz a tényleges korábbi aktivitás
   jelenik meg; később felvett kollégákhoz nem keletkezik utólagos hiányzás.

A felület a teljesítés időpontját magyarországi idő szerint jeleníti meg;
a CSV-ben az időbélyegek külön jelölt UTC értékek.

A dolgozói felületen nincs összegyűjtött olvasási idő vagy adminisztrátori
haladási kimutatás. A PDF lapozásához az aktuális oldalszám természetesen
látható. Az adatkezelési tájékoztató tényszerűen jelzi, hogy a tanulási
aktivitás és a teljesítés rögzítésre kerül.

### Évek és változatok

A közzétett tananyag PDF-je, vizsgakérdései és lezárási beállítása rögzített.
Ha ugyanabban az évben módosítás szükséges, hozz létre új tananyagot ugyanazzal
az évvel és témával: új változatszámot kap. Az új változat közzététele
archiválja a korábbi aktív változatot; annak eredményei és PDF-je megmaradnak.
Az új változat külön teljesítendő. A következő év új rekordjai nem írják
felül a korábbi évek tanulási adatait. Az adminisztrátor külön is archiválhat
közzétett anyagot. Az archivált tananyag már nem tanulható, de a kimutatásban
megmarad.

### Mit jelent az idő és az oldal?

Az aktív idő böngészőből származó becslés: a látható, fókuszban lévő
olvasóoldalon, az utolsó felhasználói aktivitástól legfeljebb 60 másodpercig
számol. Háttérbe tett lapnál, fókuszvesztésnél vagy hosszabb tétlenségnél
szünetel. A böngésző 15 másodpercenként és lapozáskor küld jelzést.
A szerver saját időbélyeggel korlátozza az elszámolható időt, elutasítja az
ismételt/sorrenden kívüli jelzéseket, és egy tananyaghoz csak egy aktív
olvasómunkamenetet tart fenn. A 45 másodpercnél hosszabb jelentési szünet
nem ír jóvá időt, egy jelzés legfeljebb 20 másodpercet számíthat hozzá.

Az oldalszám a megjelenített PDF-oldalra utal. Egy oldal megnyitása és a
böngészőben töltött idő nem bizonyítja annak elolvasását vagy megértését.
Hálózati kiesés, bezárás, eszközváltás vagy böngészőbeli védelem miatt néhány
másodperc kimaradhat. Az alkalmazás nem garantál manipulációbiztos
figyelem- vagy jelenlétmérést. A vizsga a beállított kérdésekre adott
válaszokat ellenőrzi, a helyes megoldásokat a tanulói HTML/JSON nem kapja meg.
A PDF-ből esetleg külön mentett másolat olvasását nem lehet követni.

## Telepítés

Előfeltétel a [közös fiókszolgáltatás](shared-accounts.md) telepítése és a
meglévő fiókok ellenőrzött átvezetése. A meglévő CSUPOR felhasználói és
személyügyi adatai ehhez az alkalmazáshoz nem kerülnek átmásolásra.

1. Hozz létre külön adatbázist és külön, csak arra jogosult adatbázis-felhasználót.
   A fejlesztői tesztek SQLite-ot használnak; éles telepítéshez MySQL használható.
2. Telepítsd kizárólag az új alkalmazás függőségeit a saját környezetébe:

   ```bash
   pip install -r safety/requirements.txt
   ```

3. A fiókszolgáltatás környezetében regisztráld a külön klienst. A
   `WORK_SAFETY_CLIENT_SECRET` értéke legalább 32 véletlen karakteres,
   a CSUPOR titkától eltérő titok legyen a tárhely titokkezelőjében:

   ```bash
   flask --app 'accounts:create_app()' add-client munkavedelem --name 'Tűz- és munkavédelem' --redirect-uri https://munkavedelem.example.hu/auth/callback --secret-env WORK_SAFETY_CLIENT_SECRET
   ```

4. Az új alkalmazás környezeti változóit a `safety/.env.example` szerint állítsd be.
   A fájl nem töltődik be automatikusan. A `SAFETY_OIDC_CLIENT_SECRET` az előbb
   regisztrált kliens titka. A `SAFETY_SECRET_KEY` új, egyedi titok legyen.
   Az issuer és callback pontos, saját HTTPS cím legyen.
5. A `SAFETY_UPLOAD_DIR` privát, tartós, írható könyvtár legyen, a publikus
   webgyökéren kívül. Minden alkalmazásworker ugyanazt a könyvtárat használja.
   A PDF-ek véletlen fájlnévvel kerülnek ide; csak hitelesített alkalmazásútvonal
   szolgálja ki őket. A PDF.js és összes betű-/képkezelő erőforrás helyben van,
   a tananyag nem kerül külső PDF-megjelenítő szolgáltatóhoz.

   **Konténeres tárhelyen tartós kötet szükséges.** Ideiglenes konténerlemezen
   a PDF-ek újratelepítéskor elvesznének; az ilyen tárhely önmagában nem elég
   ehhez a fájltárolási módhoz. A PDF-könyvtár és az adatbázis együtt mentendő.
6. Inicializáld a külön adatbázist:

   ```bash
   flask --app 'safety:create_app()' init-db
   ```

7. Engedélyezd a munkatársak központi hozzáférését a fiókszolgáltatásban:

   ```bash
   flask --app 'accounts:create_app()' grant-access munkavedelem <központi-fiók-UUID>
   ```

   A felhasználó az első sikeres belépéskor helyi dolgozói rekordot kap.
   A még be nem lépő munkatársak megjelenítéséhez vedd fel őket előre az
   új alkalmazás **Munkatársak** felületén vagy parancssorból, ugyanazzal
   a stabil központi fiókazonosítóval:

   ```bash
   flask --app 'safety:create_app()' add-member <központi-fiók-UUID> --name 'Munkatárs neve' --email 'munkatars@example.hu'
   ```

   Ez nem hoz létre központi fiókot és nem helyettesíti a `grant-access` lépést.
   Az e-mail-cím alapján nem történik automatikus összekapcsolás.
8. Az első adminisztrátor külön helyi jogosultsága:

   ```bash
   flask --app 'safety:create_app()' set-admin <saját-központi-fiók-UUID> --name 'Saját név' --email 'sajat@example.hu'
   ```

   A CSUPOR HR/vezetői/fejlesztői jogosultsága nem ad automatikusan hozzáférést
   a tanulási adatokhoz. További adminisztrátor ugyanígy adható hozzá;
   visszavonás: `set-admin <UUID> --remove`.
9. A tároló gyökeréből indítsd:

   ```bash
   gunicorn --bind 0.0.0.0:8000 --workers 2 'safety:create_app()'
   ```

   A HTTPS-t lezáró megbízható proxy számát a `SAFETY_PROXY_HOPS` szabályozza.
   Állapotellenőrzés: `/healthz`. A proxy/access naplóban az `/auth/callback`
   query stringjét ne őrizd meg. Éles környezetben ne használj Flask debug módot.
10. Naponta törölhetők a lejárt munkamenetek:

    ```bash
    flask --app 'safety:create_app()' cleanup
    ```

A külön adatbázis inicializálása nem futtat CSUPOR-migrációt. A helyi névsor
inaktiválása a tanulási előzményeket megtartja. A központi hozzáférés visszavonását,
a fiók tiltását és a központi kijelentkezést a következő védett kérés felismeri.
Központi szolgáltatáskimaradáskor a rendszer nem enged hozzáférést.

## Fejlesztés és ellenőrzés

Izolált helyi fejlesztéshez `SAFETY_ALLOW_HTTP=true` és kizárólag loopback
HTTP issuer/callback használható. A külön szolgáltatásokhoz külön port,
adatbázis, titkos kulcs és PDF-könyvtár szükséges.

```bash
python -m pytest safety/tests -q
node --test safety/tests/reader.test.cjs
python -m pytest tests accounts/tests -q
```

A tesztek ideiglenes adatbázissal és generált PDF-ekkel dolgoznak. A közös
belépési tesztek valódi ideiglenes fiókszolgáltatást indítanak. A tárhelyi
TLS/proxy-beállítás, a végleges adatbázis és tartós kötet ellenőrzése külön
üzembehelyezési feladat. A PR önmagában nem telepíti az alkalmazást.
