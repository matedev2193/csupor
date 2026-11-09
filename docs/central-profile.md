# Személyes profil a közös fiókban

OIDC belépés mellett a CSUPOR **Saját profil** menüje a fiókkezelő
`/profile` oldalára vezet. Itt szerkeszthető a teljes személyes adatlap,
a profilkép és annak kivágása. Az e-mail-cím és jelszó a fiókkezelő
meglévő, jelszómegerősítést kérő oldalain marad. A helyi belépési mód
régi profilszerkesztője továbbra is működik.

## Frissítés és adatmegőrzés

1. Telepítsd először a fiókkezelőt, majd a CSUPOR-t. Az új táblák
   automatikusan létrejönnek; nincs konzolparancs és nincs új kulcs.
2. A felhasználó következő hitelesített CSUPOR-kérése a stabil központi
   fiókazonosító alapján átadja a meglévő saját személyes adatokat.
   A meglévő profilképet és szerkeszthető képforrást is átvezeti.
3. Az átvezetés ismételhető: a már átvett vagy a központban szerkesztett
   mezőket és fotót nem írja felül a régi helyi másolat.
4. A központi profil lesz a szerkesztés forrása. A CSUPOR megtartja és
   frissíti a saját munkaügyi másolatát, így a kimutatások és szabadság-
   számítások továbbra is működnek. Az üresre törölt központi mezőket is
   átveszi. A helyi felhasználóazonosítók és kapcsolódó rekordok megmaradnak.

Ha egy CSUPOR-tag közvetlenül a fiókoldalon nyitja meg a még át nem
vezetett profilját, előbb a CSUPOR megnyitását kéri a felület. Így egy
üres űrlap mentése nem takarhatja el a meglévő személyes adatait.
A régi CSUPOR adminisztratív személyes adatlap OIDC módban olvasható;
a központi szerkesztéshez külön központi adminisztrátori jogosultság kell.
A CSUPOR HR-/igazgatói szerepköre önmagában nem ad ilyen jogosultságot.

## Elkülönítés

A fiókkezelő nem kapcsolódik a CSUPOR adatbázisához. A saját profil
átvezetése szerverközi kéréssel, az aktuális CSUPOR-munkamenet bearer
tokenjével történik. A fiókazonosító a hitelesített tokenből származik.
A CSUPOR a már összekapcsolt munkatársak központi profilját külön,
legfeljebb 100 azonosítót fogadó olvasási végponton frissítheti a
meglévő saját kliensazonosítójával és titkával. Ez nem enged írást.

A teljes név a szabványos OIDC `name` mezőben elérhető a `profile` scope-ot
kérő alkalmazásoknak. A név forrása a központi profil; a `/userinfo` mindig
az aktuális értéket adja vissza. Üres vagy hibás névnél ez a mező kimarad,
a felhasználónév és az állandó fiókazonosító változatlan marad.
A további személyes mezők, TAJ-, adó-, bankszámla- és egészségügyi adatok
nem kerülnek az OIDC tokenbe vagy az általános `/userinfo` válaszába.
A profil API kizárólag a fiókkezelő `OIDC_CLIENT_ID` környezeti változójával
kijelölt kliens számára érhető el; a tűz- és
munkavédelmi alkalmazás nem kapja meg ezeket az adatokat.
A képforrást csak a tulajdonos szerkesztheti. A CSUPOR a megjelenítéshez
a kis profilkép másolatát használja.

## A menük felelőse

| Funkció | Rendszer |
| --- | --- |
| Saját profil és profilkép, jelszó, belépési e-mail | Fiókkezelő |
| Regisztráció, fiókállapot, hozzáférés egy egész alkalmazáshoz | Fiókkezelő |
| Munkaügyi dolgozólista, végzettségek/képesítések és kimutatásaik | CSUPOR |
| Eltartottak, szerződések, szabadság, munkaidő és munkanapok | CSUPOR |
| Csoportok, jóváhagyás, vezetés, jogi személyek, munkahelyek | CSUPOR |
| CSUPOR-oldalak hozzáférései és szerepkörei | CSUPOR |
| HR-értesítések és levélküldés | CSUPOR |
| Fiók-helyreállítási levélküldés | Fiókkezelő |
| Tűz- és munkavédelmi telephelyek, tananyagok és haladás | Tűz- és munkavédelem |

A fiókkezelőben a **Fiókok és hozzáférések kezelése** alatt új közös fiók
hozható létre, alkalmazás-hozzáférés adható, illetve egy fiók letiltható és
újraaktiválható. A letiltás lezárja a korábbi bejelentkezéseket, a személyügyi
adatokat megőrzi. Újraaktiváláskor új bejelentkezés szükséges. Saját adminfiók
nem tiltható le, és az utolsó aktív központi adminisztrátor védett.

A **Fiókom → Elérhető rendszerek** listában az alkalmazás neve kattintható.
A hivatkozás az alkalmazás adminisztrátor által regisztrált címére vezet;
a belépéshez továbbra is az adott alkalmazásra szóló hozzáférés szükséges.

A **Fiókrendszer e-mail-beállításai** alatt a jelszó-visszaállításhoz
használt SMTP-kiszolgáló is kezelhető. A mentett jelszó titkosított, nem
jelenik meg újra. Üres jelszómező mentéskor megtartja a korábbi jelszót.
A CSUPOR-ban maradó e-mail-beállítások a személyügyi levelekhez tartoznak.

A munkaügyi modulok és rekordjaik a CSUPOR-ban maradnak. A munkahelyek
neve és címe külön, korlátozott katalógusként átvehető az oktatórendszerbe;
az átadás nem tartalmaz munkatársi adatot vagy automatikus dolgozói beosztást.
Lásd: [telephelyek átvétele](shared-workplaces.md).
