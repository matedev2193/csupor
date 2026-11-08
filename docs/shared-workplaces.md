# CSUPOR-munkahelyek átvétele a tűz- és munkavédelembe

A munkahelyeket továbbra is a CSUPOR-ban kell karbantartani. Az oktatórendszer
a munkahely állandó azonosítóját, megjelenítési nevét és címét veheti át.
A megjelenítési név a jogi személy nevéből és a munkahely címéből áll.
Azonos vagy túl hosszú névnél az azonosító segít megkülönböztetni a helyeket.

Az adatút: **CSUPOR → közös fiókkezelő → tűz- és munkavédelem**.
Mindhárom rendszer a saját adatbázisát használja. A katalógus nem tartalmaz
dolgozókat, szerződéseket, adószámokat vagy képzési adatokat. Az oktatórendszer
nem kap CSUPOR-adatbázis-hozzáférést, és nem importál CSUPOR-forráskódot.

## Használat

1. Az elfogadott verziókból frissítsd a fiókkezelőt, a CSUPOR-t, majd az
   önálló munkavédelmi alkalmazást. Új környezeti változó vagy titkos kulcs
   nem szükséges, az új táblák induláskor létrejönnek.
2. Nyisd meg bejelentkezve a CSUPOR-t. Az alkalmazás automatikusan elküldi a
   katalógust, később munkahely/jogi személy mentése után is frissíti.
3. A tűz- és munkavédelmi rendszerben helyi adminisztrátorként nyisd meg:
   **Telephelyek → Átvétel / frissítés a CSUPOR-ból**.
4. Az előnézetben ellenőrizd a helyeket. Egy CSUPOR-munkahelyhez meglévő
   helyi telephelyet választhatsz, vagy új telephelyet hozhatsz létre.
   Egy helyi telephely csak egy forráshelyhez kapcsolható.
5. Hagyd jóvá az átvételt. A későbbi frissítés ugyanazokat a helyi
   telephelyeket használja, így a tananyagok és munkatársi hozzárendelések
   megmaradnak. A forrásból eltűnt helyek nem törlődnek automatikusan.

A kézzel módosított helyi telephelynév megmarad. Az előnézet után változó
forrás- vagy helyi adat esetén új ellenőrzés szükséges; részleges átvétel
nem mentődik. A CSUPOR elérhetetlen fiókkezelő mellett is menti a saját
munkahelyeit, és a katalógus küldését későbbi látogatáskor újrapróbálja.

## Kapcsolat és verziózás

A CSUPOR a meglévő `OIDC_ISSUER`, `OIDC_CLIENT_ID=csupor` és
`OIDC_CLIENT_SECRET` beállításaival publikál a `/api/csupor/workplaces`
végpontra. Monoton növekvő generáció akadályozza meg, hogy egy később beérő
régi kérés felülírja az újabb listát.

A `/api/workplaces` végpont csak a `munkavedelem` vagy `tuzesmunkavedelem` kliens érvényes,
`openid profile` jogosultságú hozzáférési tokenjével olvasható. A safety
alkalmazás ezt a meglévő szerveroldali bejelentkezéséből veszi. A kérés
a beállított fiókkezelőre megy, átirányítást nem követ. A lista legfeljebb
1000 helyet és 2 MiB adatot tartalmazhat. Az első publikálás előtt a
felület jelzi, hogy előbb a frissített CSUPOR-t kell megnyitni.
