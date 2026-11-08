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

A személyes mezők, TAJ-, adó-, bankszámla- és egészségügyi adatok nem
kerülnek az OIDC tokenbe vagy az általános `/userinfo` válaszába.
A profil API kizárólag a `csupor` kliens számára érhető el; a tűz- és
munkavédelmi alkalmazás nem kapja meg ezeket az adatokat.
A képforrást csak a tulajdonos szerkesztheti. A CSUPOR a megjelenítéshez
a kis profilkép másolatát használja.

## A menük ajánlott felelőse

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

Az aloldalak besorolása javaslat: a profil áthelyezése nem mozgatja át
a többi felsorolt modult vagy a hozzájuk tartozó munkaügyi rekordokat.
