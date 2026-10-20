# Közös Micimackó-fiók és önálló CSUPOR

A `accounts/` egy külön futtatható fiókszolgáltatás saját adatbázissal,
saját titkos kulcsokkal és saját böngészős munkamenettel. A CSUPOR ennek
OpenID Connect kliense. A forrásuk egy tárolóban van, de két külön alkalmazásként
kell őket telepíteni; a fiókszolgáltatás nem importálja a CSUPOR kódját és nem
kap hozzáférést a személyügyi adatbázishoz.

| Adat vagy feladat | Felelős rendszer |
| --- | --- |
| Felhasználónév, belépési e-mail, jelszó, jelszó-visszaállítás | Micimackó-fiók |
| Állandó fiókazonosító és alkalmazáshoz való hozzáférés | Micimackó-fiók |
| Dolgozói profil, szerződések, szabadság, munkaidő, dokumentumok | CSUPOR |
| CSUPOR employee / HR / igazgató / fejlesztő jogosultság | CSUPOR |
| Másik alkalmazás saját szerepkörei és adatai | Az adott alkalmazás |

A belépés mindegyik alkalmazásból a közös fiókoldalra vezet. Ha ott már
bejelentkeztél, a következő alkalmazásba újabb jelszóbeírás nélkül léphetsz be,
ha ahhoz is kaptál hozzáférést. A központi fiók önmagában nem ad jogosultságot
az összes alkalmazáshoz. A CSUPOR új, engedélyezett fióknak kizárólag
`employee` szerepkört és üres profilt hoz létre.

## Üzembe helyezés két alkalmazással

Példák: `https://fiok.example.hu` és `https://csupor.example.hu`. Ezeket saját,
HTTPS-t használó címekkel helyettesítsd. Az issuer a fiókszolgáltatás teljes
eredetcíme, útvonal és záró `/` nélkül; az átállás után ne változtasd meg.
Másik issuer új identitást jelent, ezért nem egyszerű domainátnevezés.

Mindkét alkalmazáshoz külön adatbázist és külön, csak a saját adatbázisára
jogosult adatbázis-felhasználót használj. A CSUPOR `DATABASE_URL` beállítása
változatlan marad. A központi szolgáltatásé `ACCOUNTS_DATABASE_URL`.

1. A fiókszolgáltatás függőségei:

   ```bash
   pip install -r accounts/requirements.txt
   python -m accounts.keygen /private/accounts-signing-key.pem
   ```

   A privát kulcsot tartós, csak a szolgáltatás számára olvasható secret mounton
   tárold, és készíts róla védett mentést. Ne kerüljön Gitbe vagy ephemeral
   konténerfájlrendszerbe. Minden worker ugyanazt a kulcsot használja.

2. Állítsd be az `accounts/.env.example` változóit a fiókszolgáltatás
   tárhelyén. A szolgáltatás a környezeti változókból olvas; az `.env.example`
   nem töltődik be magától. Legalább:

   ```text
   ACCOUNTS_DATABASE_URL=mysql+mysqlconnector://accounts_user:...@host/micimacko_accounts
   ACCOUNTS_SECRET_KEY=<külön, véletlen, legalább 32 karakteres titok>
   ACCOUNTS_ISSUER=https://fiok.example.hu
   ACCOUNTS_SIGNING_KEY_FILE=/private/accounts-signing-key.pem
   ACCOUNTS_TOKEN_TTL=3600
   ACCOUNTS_ALLOW_HTTP=false
   ```

   A jelszó-visszaállításhoz itt is be kell állítani a saját SMTP-változókat
   (`ACCOUNTS_SMTP_*`). A CSUPOR-ban tárolt SMTP-beállításokat a külön
   szolgáltatás nem olvassa. A közös fiókhoz tartozó levelek innen indulnak,
   a személyügyi értesítések továbbra is a CSUPOR-ból.

3. Hozd létre a táblákat és a CSUPOR-klienst. Az `ACCOUNTS_CLIENT_SECRET`
   környezeti változóba előzőleg tegyél egy külön, legalább 32 karakteres
   véletlen titkot a tárhely titokkezelőjében. Ne használj mintajelszót.

   ```bash
   flask --app 'accounts:create_app()' init-db
   flask --app 'accounts:create_app()' add-client csupor --name CSUPOR --redirect-uri https://csupor.example.hu/auth/callback --secret-env ACCOUNTS_CLIENT_SECRET
   ```

   A `--allow-registration` kapcsolóval engedélyezhető a CSUPOR-ból indított
   nyilvános regisztráció. Alapértelmezésben zárt a regisztráció. A kapcsolót
   csak azon klienshez add meg, amelynél ez a kívánt működés. Az importált
   fiókoknak nincs szükségük új regisztrációra.

4. Indítás a projekt gyökérkönyvtárából:

   ```bash
   gunicorn --bind 0.0.0.0:8000 'accounts:create_app()'
   ```

   A publikus forgalmat HTTPS végponton fogadó reverse proxy továbbítsa a
   szolgáltatásnak. Ha a TLS egyetlen megbízható proxyban végződik, és az
   állítja be az `X-Forwarded-Proto` fejlécet, legyen `ACCOUNTS_PROXY_HOPS=1`;
   közvetlen HTTPS kiszolgálásnál az alapértelmezett `0` maradjon. A backend
   csak a megadott számú megbízható proxy felől legyen elérhető. A szolgáltatás
   éles módban visszautasítja a nem HTTPS-ként azonosított kéréseket.
   A tárhely belső állapotellenőrzéséhez a `/healthz` útvonalat használd.
   A többi végpont csak a konfigurált issuer címén fogad kérést.
   A callback és issuer címek rögzítettek, nem a kérés
   `Host` fejlécéből származnak. A proxy/access logokban az `/authorize`,
   `/auth/callback` és `/reset-password/` útvonalaknál ne őrizd meg a query
   stringet vagy visszaállító tokent. Az alkalmazás nem naplóz jelszót vagy
   tokent. Nyilvános környezetben a Flask debug mód tilos.

5. A lejárt központi tokenek és korlátozási számlálók takarítása naponta:

   ```bash
   flask --app 'accounts:create_app()' cleanup
   ```

## Meglévő CSUPOR-fiókok átállítása

Az új verzió alapértelmezése `AUTH_MODE=local`, ezért pusztán a kód telepítése
nem változtatja meg a belépést. A két új CSUPOR-táblát az indulási folyamat
automatikusan létrehozza, a meglévő rekordok és oszlopok változatlanok.

Az átállást rövid karbantartási ablakban végezd:

1. Készíts ellenőrzött mentést a CSUPOR adatbázisáról. Állítsd le a CSUPOR
   web- és háttérfolyamatait, valamint az új fiókszolgáltatás webes elérését.
   Az export és import között ne lehessen regisztrálni, jelszót vagy e-mailt
   változtatni. Két külön adatbázis között nincs közös tranzakció.
2. A CSUPOR környezetében, még `AUTH_MODE=local` mellett:

   ```bash
   flask --app run accounts export --issuer https://fiok.example.hu --output /private/migration.accounts-export.json
   ```

   A parancs állandó, véletlen fiókazonosítót rendel minden meglévő helyi
   felhasználóhoz. A meglévő CSUPOR-azonosító, jogosultság és személyügyi
   kapcsolat megmarad. Az export kizárólag a fiókazonosítót, felhasználónevet,
   e-mailt és meglévő jelszólenyomatot tartalmazza. A jelszavakat nem kell
   visszafejteni vagy újra bekérni. A fájl `0600` jogosultsággal készül;
   létező fájlt nem ír felül. A fájlnév vége kötelezően `.accounts-export.json`.
3. Juttasd el a privát exportot biztonságosan a fiókszolgáltatás
   környezetébe, majd ott:

   ```bash
   flask --app 'accounts:create_app()' import-users /private/migration.accounts-export.json --client-id csupor
   ```

   Az import atomikus, megtartja a jelszólenyomatokat, és csak a CSUPOR-hoz
   ad hozzáférést. Az azonos tartalmú ismételt import megengedett; az eltérő
   vagy ütköző fiókok nem írhatók felül. A CSUPOR-jogosultságok nem kerülnek
   a központi adatbázisba. Az importált e-mail nem válik automatikusan
   ellenőrzött tulajdonú e-mail-címmé.
4. A CSUPOR környezetében állítsd be:

   ```text
   AUTH_MODE=oidc
   OIDC_ISSUER=https://fiok.example.hu
   OIDC_CLIENT_ID=csupor
   OIDC_CLIENT_SECRET=<az add-client során használt titok>
   OIDC_REDIRECT_URI=https://csupor.example.hu/auth/callback
   OIDC_SESSION_TTL=3600
   SECRET_KEY=<a CSUPOR saját, legalább 32 karakteres titka>
   ```

   A CSUPOR `SECRET_KEY` kulcsa legyen eltérő a fiókszolgáltatás kulcsától.
   Az `INITIAL_ADMIN_*` változókat távolítsd el: OIDC módban a helyi
   adminisztrátor-létrehozás nem fut le. Az átvezetett fejlesztői fiók
   meglévő jogosultsága megmarad.
5. Indítsd újra a két alkalmazást. Tesztelj legalább egy meglévő dolgozói és
   egy fejlesztői fiókot a régi jelszavukkal: belépés, profil, jogosultság,
   jelszó-visszaállító levél, kijelentkezés. Ellenőrizd a második alkalmazást
   is, ha már csatlakoztatva van. A korábbi helyi böngészős munkamenetek
   nem használhatók a központi belépés helyett.
6. Csak a sikeres import és valódi belépések ellenőrzése után törölhetők a
   CSUPOR-ban megmaradt helyi jelszólenyomatok:

   ```bash
   flask --app run accounts retire-local-passwords --confirm-imported-and-tested
   ```

   Ez a parancs visszafordíthatatlanul lecseréli a helyi jelszómásolatokat
   belépésre alkalmatlan értékre, és törli a régi CSUPOR reset-tokeneket.
   Az ellenőrzésről az üzemeltető nyilatkozik a kapcsolóval; a parancs nem
   vizsgálja meg közvetlenül a másik adatbázis importjának sikerességét.
   Minden helyi felhasználóhoz aktív, a konfigurált issuerhez tartozó
   azonosítókapcsolat szükséges. A központi jelszavakat és a személyügyi
   adatokat nem módosítja. Ezután az `AUTH_MODE=local` visszaállítása nem
   jelent működő visszaállási tervet.
7. Távolítsd el a migrációs exportot mindkét gépről, és a benne lévő
   jelszólenyomatokat tartalmazó mentéseket a saját védett megőrzési rend
   szerint kezeld. Exportot, privát kulcsot vagy `.env` fájlt ne tölts fel
   GitHubra, hibajegybe vagy PR-ba.

Hiba esetén az átállást ne folytasd részleges adatokkal. Az export előtti
adatbázismentés adja a helyi visszaállási pontot. Ha a központi fiókokat már
használták és ott megváltoztatták a jelszavakat, a régi mentés visszatöltése
nem őrzi meg ezeket a változásokat; ilyenkor külön egyeztetett helyreállítás
szükséges.

## Másik rendszer csatlakoztatása

Hozz létre új klienst külön titokkal és az adott rendszer pontos callback
címével. A titkot ugyanúgy a tárhely környezeti titokkezelőjébe tedd:

```bash
flask --app 'accounts:create_app()' add-client munkavedelem --name 'Tűz- és munkavédelem' --redirect-uri https://munkavedelem.example.hu/auth/callback --secret-env WORK_SAFETY_CLIENT_SECRET
flask --app 'accounts:create_app()' grant-access munkavedelem <központi-fiók-UUID>
```

Az új rendszer szabványos OIDC klienskönyvtárával az alábbi szerződést használd:

| Beállítás | Érték |
| --- | --- |
| Discovery | `https://fiok.example.hu/.well-known/openid-configuration` |
| Flow | Authorization Code + kötelező PKCE S256 |
| Klienshitelesítés | `client_secret_basic` |
| Scope | `openid profile email` |
| ID token aláírás | RS256; publikus kulcsok a discovery `jwks_uri` címén |
| Felhasználó kulcsa | az ellenőrzött `iss` és `sub` együtt |
| Átadott adatok | `sub`, `preferred_username`, `email`, `email_verified` |
| Nem átadott adatok | jelszó, jelszólenyomat, személyügyi adatok, CSUPOR-szerepkör |

Az állandó `sub` azonosítót tárold a saját helyi profilhoz. Az e-mail alapján
ne kapcsolj automatikusan össze meglévő fiókokat. Minden callbacknél szükséges
a state, nonce, aláírás, issuer, audience és lejárat ellenőrzése. A működő
CSUPOR-példa az `app/shared_auth.py` fájlban található. A másik rendszerhez
külön kell megvalósítani ezt az integrációt; a kliens regisztrálása önmagában
nem módosítja egy másik alkalmazás kódját.

## Kijelentkezés, visszavonás és fiókmódosítás

- A CSUPOR kiléptet, majd a közös fiók kijelentkezési megerősítésére vezet.
  A központi kijelentkezés POST-megerősítése visszavonja a központi
  munkameneteket és tokeneket. A többi rendszernek is ellenőriznie kell a
  token állapotát, hogy ezt észrevegye. A CSUPOR minden hitelesített
  kéréskor meghívja a `/userinfo` végpontot; elérhetetlenség vagy visszavonás
  esetén nem enged hozzáférést.
- A CSUPOR böngészős sütijében csak véletlen munkamenet-azonosító van;
  a hozzáférési token a CSUPOR adatbázisában titkosítva tárolódik. A
  fiókszolgáltatás csak a bearer token lenyomatát tárolja. A jelszócsere és
  jelszó-visszaállítás érvényteleníti a régi központi munkameneteket.
- A fiókbeállításokat és a jelszót a központi felületen lehet módosítani.
  OIDC módban a CSUPOR helyi belépési, regisztrációs és reset végpontjai nem
  fogadnak el helyi hitelesítő adatokat. A személyügyi profil továbbra is
  a CSUPOR-ban szerkeszthető.
- A CSUPOR az értesítési e-mail másolatát a következő sikeres központi
  belépéskor frissíti. A központi e-mailcsere után lépj vissza a CSUPOR-ba;
  addig a háttérértesítések a korábban tárolt címre mehetnek. Ez nem valós
  idejű központi címtárszinkronizáció.
- A CSUPOR-profil törlése friss központi jelszóhitelesítést és külön
  megerősítést kér. A központi fiók megmarad, a CSUPOR-ban pedig tiltó
  azonosítókapcsolat marad, így egy későbbi belépés nem hozza vissza
  automatikusan a törölt profilt.

Központi adminisztráció, kizárólag a fiókszolgáltatás parancssorában:

```bash
flask --app 'accounts:create_app()' revoke-access munkavedelem <központi-fiók-UUID>
flask --app 'accounts:create_app()' disable-user <központi-fiók-UUID>
```

Az első parancs csak az adott alkalmazás jogosultságát vonja vissza; a
második a teljes központi fiókot tiltja le. Ezek nem törlik a CSUPOR HR-adatait.

## Fejlesztés és ellenőrzés

Éles környezetben HTTPS szükséges. Izolált, helyi fejlesztéshez használható
`ACCOUNTS_ALLOW_HTTP=true` a szolgáltatásban és `AUTH_ALLOW_HTTP=true` a
CSUPOR-ban, kizárólag `localhost`, `127.0.0.1` vagy `::1` címekkel.
Külön portot, SQLite-fájlt és titkos kulcsot használj mindkét alkalmazáshoz.

```bash
python -m pytest tests/test_account_migration.py tests/test_shared_auth.py tests/test_shared_accounts_e2e.py accounts/tests -q
python -m pytest tests -q
```

A tesztek ideiglenes adatbázisokkal és helyi HTTP-szerverrel futnak, éles
felhasználói adatok nélkül. A MySQL indulási séma a meglévő signed/unsigned
azonosítókat a CSUPOR saját bootstrap eljárásával követi. A valódi tárhely,
SMTP és a végleges HTTPS címek átállási ellenőrzése külön telepítési lépés.
