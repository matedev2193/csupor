# Közös fiókrendszer DigitalOceanön, konzolparancsok nélkül

A CSUPOR a `https://csupor.matedev.hu` címen marad. A belépés a külön
`https://fiok.matedev.hu` fiókoldalra vezet, majd visszatér az alkalmazásba.
Ugyanez a fiók használható az önálló tűz- és munkavédelmi rendszerben,
ha ahhoz hozzáférést engedélyezel.

| Rendszer | Saját adatok |
| --- | --- |
| CSUPOR | Meglévő felhasználóazonosítók, személyügyi rekordok és helyi jogosultságok |
| Micimackó-fiók | Belépési név, e-mail, jelszó és alkalmazás-hozzáférések |
| Tűz- és munkavédelem | Telephelyek, tananyagok, PDF-ek, haladás és helyi adminjog |

Mindegyik külön alkalmazás és adatbázis. Az `accounts/` forrás a CSUPOR
repóban van, de a külön Docker-image csak a fiókszolgáltatást tartalmazza.
A fiókszolgáltatás nem olvassa a CSUPOR adatbázisát.

## 1. Az új fiókalkalmazás telepítése

A fejlesztési PR célága `develop`. Az alábbi éles lépéseket az elfogadott
verzió `main` ágra kerülése után végezd el. A kód telepítése önmagában nem
kapcsolja át a CSUPOR belépését.

A DigitalOceanön hozz létre **új Appot** a fiókszolgáltatásnak:

| Mező | Érték |
| --- | --- |
| Repository | `matedev2193/csupor` |
| Branch | `main`, az elfogadott változattal |
| Source directory | `/` |
| Dockerfile path | `accounts/Dockerfile` |
| Build command | üres; a Dockerfile végzi |
| Run command | `python -m accounts.start` (a Dockerfile is tartalmazza) |
| HTTP port | `8080` |
| Health check HTTP path | `/healthz` |

Az `.do/accounts-app.yaml` importálható beállításminta. Az új alkalmazás és
adatbázis díját a DigitalOcean a létrehozás előtt mutatja.

Hozz létre külön, tartós MySQL-adatbázist a szolgáltató webes felületén, és
engedélyezd az alkalmazás hálózati hozzáférését. Az adatbázis-felhasználó a
saját adatbázisában kapjon tábla- és indexlétrehozási jogot is.

Az **Environment Variables** mezőket **Run Time** hatókörrel töltsd ki:

| Változó | Érték | Secret |
| --- | --- | --- |
| `ACCOUNTS_DATABASE_URL` | Pl. `mysql://USER:PASSWORD@HOST:25060/fiok?ssl-mode=REQUIRED` | igen |
| `ACCOUNTS_DATABASE_CA_CERT` | DigitalOcean Managed MySQL esetén a letöltött CA-tanúsítvány teljes PEM-szövege | nem |
| `ACCOUNTS_SECRET_KEY` | Legalább 32 karakteres véletlen kulcs; új deploynál is őrizd meg változatlanul | igen |
| `ACCOUNTS_SETUP_TOKEN` | Másik, legalább 32 karakteres véletlen kulcs az első beállításhoz | igen |
| `ACCOUNTS_ISSUER` | `https://fiok.matedev.hu` | nem |
| `ACCOUNTS_PROXY_HOPS` | `1` | nem |

A kulcsokat jelszókezelővel is generálhatod. Az adatbázis-URL-ben a speciális
karakterek legyenek URL-kódolva. A MySQL CA-tanúsítvány a DigitalOcean
**Databases → adatbázis → Connection Details → Download CA certificate**
lehetőségénél tölthető le. A kapcsolat tanúsítványt és kiszolgálónevet is ellenőriz.

Induláskor elkészülnek a táblák és az aláírókulcs. Új telepítéshez nem kell
kulcsfájlt generálni vagy csatolni: az egyszer létrehozott RSA-kulcs titkosítva
a fiókadatbázisban marad. A használatához az eredeti `ACCOUNTS_SECRET_KEY`
kell. Az adatbázisról és erről a kulcsról is legyen védett mentés.
Hibás visszafejtésnél az alkalmazás leáll, nem generál másik aláírókulcsot.

## 2. A fiok.matedev.hu hozzárendelése

A fiókalkalmazásnál: **Networking → Domains → Add domain → fiok.matedev.hu**.
A **You manage your domain** lehetőségnél másold ki az `ondigitalocean.app`
végű CNAME-célcímet. A `matedev.hu` DNS-kezelőjénél vegyél fel egy `CNAME`
rekordot `fiok` névvel, erre a célra mutatva. Várd meg a domain és a HTTPS
aktiválását. Az első beállítás címe: `https://fiok.matedev.hu/setup`.

## 3. A meglévő CSUPOR-fiókok exportálása

Tervezd az átvezetést rövid karbantartási időre. Előtte készíts ellenőrzött
adatbázismentést. Az export és a közös belépés bekapcsolása között a CSUPOR
normál működése szünetel, hogy a fiókok adatai ne változhassanak meg közben.

1. A **CSUPOR** HTTP health check útvonala legyen `/healthz`.
2. A CSUPOR környezeti változói között állítsd be:
   `ACCOUNTS_MIGRATION_MODE=true`. Az `AUTH_MODE` még maradjon `local`.
3. Indíts új deployt, és **várd meg az összes régi deployment/worker leállását**.
   Csak az új folyamatok tudják érvényesíteni a karbantartást. Ezt a böngészős
   export helyetted nem tudja ellenőrizni.
4. Nyisd meg a `https://csupor.matedev.hu/developer/shared-accounts` oldalt,
   és lépj be a meglévő fejlesztői fiókoddal.
5. Add meg az issuert (`https://fiok.matedev.hu`), a jelenlegi jelszavadat
   és egy külön, legalább 32 karakteres **átviteli kulcsot**. Erősítsd meg,
   hogy a régi folyamatok leálltak, majd töltsd le a titkosított exportot.

A fájl fiókazonosítókat, belépési neveket, e-maileket és a meglévő
jelszólenyomatokat tartalmazza titkosítva. Személyügyi rekordokat nem másol.
A CSUPOR eredeti felhasználóazonosítói, jogosultságai és adatkapcsolatai
megmaradnak. A kollégák a régi jelszavukat használhatják.

Őrizd meg az átviteli kulcsot az importig. A fájlt és a kulcsot ne töltsd fel
GitHubra vagy hibajegybe. Az export 24 órán át importálható; lejáratkor még
karbantartási módban készíts új exportot.

## 4. Import és az első központi adminisztrátor

A `https://fiok.matedev.hu/setup` oldalon add meg:

- Az `ACCOUNTS_SETUP_TOKEN` értékét.
- A titkosított exportfájlt és az átviteli kulcsot.
- Saját meglévő fiókod belépési nevét/e-mailjét és jelszavát.
- A CSUPOR visszatérési címét: `https://csupor.matedev.hu/auth/callback`.
- A CSUPOR külön klienskulcsát. A felületen generálhatsz véletlen kulcsot;
  mentsd el jelszókezelőben, mert ugyanez kell majd az `OIDC_CLIENT_SECRET` mezőbe.

Sikeres importtal a saját közös fiókod központi adminjogot kap. Minden
importált fiók kap CSUPOR-hozzáférést, a többi alkalmazás külön engedélyezhető.
Hibás kulcs, jelszó, issuer vagy azonosítóütközés esetén a teljes beállítás
visszavonódik, meglévő fiókot nem ír felül. A sikeres első beállítás egyszeri.

## 5. A CSUPOR átkapcsolása

Csak a sikeres import után módosítsd a **CSUPOR** környezeti változóit:

```text
AUTH_MODE=oidc
OIDC_ISSUER=https://fiok.matedev.hu
OIDC_CLIENT_ID=csupor
OIDC_REDIRECT_URI=https://csupor.matedev.hu/auth/callback
ACCOUNTS_MIGRATION_MODE=false
```

Az `OIDC_CLIENT_SECRET` titkos mezőbe a 4. lépésben választott CSUPOR-klienskulcs
kerüljön. A CSUPOR saját `SECRET_KEY` és adatbázis-beállításai maradjanak meg.
Az `INITIAL_ADMIN_*` mezőket töröld, ha még szerepelnek.

Deploy után a `https://csupor.matedev.hu/login` a közös fiókoldalra vezet.
Próbáld ki a saját és egy munkatársi fiók belépését a meglévő jelszóval;
ellenőrizd a szerepköröket, profilokat és személyügyi adatokat.

A webes átvezetés nem törli a régi helyi jelszólenyomatokat. A központi
jelszó- és e-mail-módosítások ezután a fiókrendszerben érvényesek. Ne indítsd
újra párhuzamosan a régi helyi bejelentkezést: az `AUTH_MODE=local` önmagában
nem szinkronizálja vissza a változásokat. Visszaállásnál előbb állítsd le a
központi fiókmódosításokat és egyeztesd az adatállapotokat.

## 6. A tűz- és munkavédelem csatlakoztatása

A `https://fiok.matedev.hu/admin` oldalon hozz létre új alkalmazást:

| Mező | Érték |
| --- | --- |
| Kliensazonosító | `munkavedelem` |
| Név | `Tűz- és munkavédelem` |
| Visszatérési cím | Az oktatórendszer tényleges HTTPS-címe + `/auth/callback` |
| Klienskulcs | Külön generált kulcs; őrizd meg az oktatórendszer beállításához |
| Nyilvános regisztráció | kikapcsolva |

A fiókoknál engedélyezd magadnak és a kollégáknak a `munkavedelem` hozzáférést.
Az **oktatórendszer** környezeti változói:

```text
SAFETY_OIDC_ISSUER=https://fiok.matedev.hu
SAFETY_OIDC_CLIENT_ID=munkavedelem
```

A `SAFETY_OIDC_CLIENT_SECRET` az ehhez a klienshez választott titok;
a `SAFETY_OIDC_REDIRECT_URI` az itt regisztrált pontos visszatérési cím.
A központi adminfelület megmutatja a klienshez tartozó változóneveket és címeket.
A titkokat lenyomatként tárolja, később nem olvashatók vissza. Új titoknál
a kapcsolódó alkalmazás környezeti változóját is frissítsd.

Ezután az oktatórendszerben a közös fiókkal bejelentkezhetsz, és a saját
`/setup` oldalán, a `SAFETY_SETUP_TOKEN` segítségével helyi adminjogot kaphatsz.
A telephelyek és tananyagok továbbra is az oktatórendszerhez tartoznak.

## Levelezés és további üzemeltetés

A közös jelszó-visszaállításhoz töltsd ki a fiókalkalmazás `ACCOUNTS_SMTP_*`
változóit is. A CSUPOR levélküldése továbbra is a személyügyi értesítéseket
kezeli. Az első sikeres beállítás után az `ACCOUNTS_SETUP_TOKEN` törölhető;
ez nem nyitja újra az adminbeállítást.

Az issuer az állandó fiókazonosság része; később ne nevezd át egyszerű
domaincserével. Mentsd külön az adatbázisokat és a stabil titkos kulcsokat.
A kézi üzemeltetés dokumentációja: [technikai útmutató](shared-accounts.md).

Platformleírások: [DigitalOcean domainbeállítás](https://docs.digitalocean.com/products/app-platform/how-to/manage-domains/),
[MySQL és CA-tanúsítvány](https://docs.digitalocean.com/products/databases/mysql/how-to/connect/),
[Dockerfile beállítás](https://docs.digitalocean.com/products/app-platform/reference/dockerfile/).
