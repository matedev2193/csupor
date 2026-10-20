"""Allowlisted startup diagnostics; never format driver errors or config values."""
import ssl

from mysql.connector.errors import Error as MySQLError
from sqlalchemy.exc import ArgumentError, DBAPIError, SQLAlchemyError


class StartupError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


_MESSAGES = {
    'ACCOUNTS_ISSUER': 'Az ACCOUNTS_ISSUER hiányzik vagy hibás. Teljes HTTPS-alapcímet adj meg, például https://fiok.matedev.hu, útvonal nélkül.',
    'ACCOUNTS_SECRET_KEY': 'Az ACCOUNTS_SECRET_KEY hiányzik vagy 32 karakternél rövidebb. Meglévő adatbázisnál az eredeti kulcsot állítsd vissza.',
    'ACCOUNTS_SETUP_TOKEN': 'Az ACCOUNTS_SETUP_TOKEN megadott értékének 32–1024 karakteresnek kell lennie.',
    'ACCOUNTS_TOKEN_TTL': 'Az ACCOUNTS_TOKEN_TTL egész szám legyen 60 és 3600 között; az alapérték 3600.',
    'ACCOUNTS_PROXY_HOPS': 'Az ACCOUNTS_PROXY_HOPS értéke 0, 1 vagy 2 legyen. A dokumentált DigitalOcean-beállítás: 1.',
    'ACCOUNTS_DATABASE_URL': 'Az ACCOUNTS_DATABASE_URL hiányzik, hibás vagy nem támogatott. A teljes MySQL-kapcsolati URL-t add meg, adatbázisnévvel; a jelszó speciális karaktereit URL-kódold. Támogatott: mysql://, mysql+mysqlconnector://, helyi használathoz sqlite://.',
    'ACCOUNTS_DATABASE_CA_CERT': 'Az ACCOUNTS_DATABASE_CA_CERT nem érvényes PEM-tanúsítvány. A letöltött adatbázis-CA teljes szövegét add meg, a BEGIN és END CERTIFICATE sorokkal együtt.',
    'DATABASE_CA_FILE': 'Az adatbázis CA-tanúsítványának ideiglenes fájlja nem írható. Ellenőrizd az alkalmazás ideiglenes könyvtárának írhatóságát.',
    'ACCOUNTS_SIGNING_KEY_FILE': 'Az ACCOUNTS_SIGNING_KEY_FILE által megadott RSA-kulcsfájl nem olvasható vagy érvénytelen. Új, adatbázisban tárolt kulcsot használó telepítéshez ezt a változót nem kell megadni; meglévő kulcsfájlos telepítésnél az eredeti fájlt állítsd helyre.',
    'SIGNING_KEY': 'A tárolt aláírókulcs nem fejthető vissza. Ellenőrizd az eredeti ACCOUNTS_SECRET_KEY értékét és a hozzá tartozó adatbázist. Nem cseréltünk és nem töröltünk kulcsot.',
    'DATABASE_LOCK': 'Az adatbázis előkészítését másik folyamat zárolja. Várd meg annak befejezését, majd próbáld újra a telepítést.',
    'DATABASE_AUTH': 'Az adatbázis elutasította a hitelesítést. Ellenőrizd az ACCOUNTS_DATABASE_URL felhasználónevét, jelszavát és a felhasználó hitelesítési beállításait.',
    'DATABASE_NAME': 'A kapcsolatban megadott adatbázis nem létezik. Hozd létre a fiókszolgáltatás saját adatbázisát, és annak nevét add meg az ACCOUNTS_DATABASE_URL-ben.',
    'DATABASE_PERMISSION': 'Az adatbázis-felhasználónak nincs elegendő jogosultsága. Engedélyezd a fiókszolgáltatás saját adatbázisának elérését, a táblák/indexek létrehozását és az adatok kezelését.',
    'DATABASE_TLS': 'Az adatbázis titkosított kapcsolata vagy tanúsítvány-ellenőrzése sikertelen. Ellenőrizd a kiszolgálónevet és az ACCOUNTS_DATABASE_CA_CERT teljes PEM-szövegét; a TLS-ellenőrzést ne kapcsold ki.',
    'DATABASE_NETWORK': 'Az adatbázis nem érhető el vagy a kapcsolat megszakadt. Ellenőrizd a hostot, portot és az alkalmazás engedélyezését az adatbázis Trusted Sources beállításánál.',
    'DATABASE_CAPACITY': 'Az adatbázis kapcsolati korlátja betelt. Ellenőrizd az aktív kapcsolatokat és az adatbázis kapacitását.',
    'DATABASE_BUSY': 'Az adatbázis-művelet zárolás vagy ütköző tranzakció miatt nem fejeződött be. Az ütköző művelet után próbáld újra a telepítést.',
    'DATABASE_SCHEMA': 'Az adatbázis előkészítése során a séma vagy az SQL-művelet hibát jelzett. Őrizd meg az adatbázist; a telepített verzió és a séma ellenőrzése szükséges.',
    'DATABASE': 'Az adatbázis előkészítése sikertelen. Ellenőrizd az adatbázis elérhetőségét, jogosultságait és a telepített verzióhoz tartozó sémát.',
    'PORT': 'A PORT egész szám legyen 1 és 65535 között. A dokumentált DigitalOcean-beállítás: 8080.',
    'DEPENDENCY': 'Az indításhoz szükséges Python-függőség nem tölthető be. Az accounts/Dockerfile használatával készíts új buildet.',
    'UNEXPECTED': 'Az előkészítés váratlan hibával leállt. A telepített verzió és a beállítások vizsgálata szükséges; a napló szándékosan nem ír ki titkos értékeket.',
}

_MYSQL_CODES = {
    **dict.fromkeys((1045, 1698, 2061), 'DATABASE_AUTH'),
    1049: 'DATABASE_NAME',
    **dict.fromkeys((1044, 1142, 1143, 1227), 'DATABASE_PERMISSION'),
    **dict.fromkeys((2026, 3159), 'DATABASE_TLS'),
    **dict.fromkeys((2002, 2003, 2005, 2006, 2013, 2055), 'DATABASE_NETWORK'),
    1040: 'DATABASE_CAPACITY',
    **dict.fromkeys((1205, 1213), 'DATABASE_BUSY'),
    **dict.fromkeys((1054, 1146, 1064), 'DATABASE_SCHEMA'),
}


def diagnostic(error):
    """Choose a constant message; do not interpolate exception text or errno."""
    code = 'UNEXPECTED'
    if isinstance(error, StartupError):
        code = error.code if error.code in _MESSAGES else 'UNEXPECTED'
    else:
        original = error.orig if isinstance(error, DBAPIError) else error
        if isinstance(original, MySQLError):
            code = _MYSQL_CODES.get(original.errno, 'DATABASE')
        elif isinstance(original, ssl.SSLError):
            code = 'DATABASE_TLS'
        elif isinstance(error, ArgumentError):
            code = 'ACCOUNTS_DATABASE_URL'
        elif isinstance(error, SQLAlchemyError):
            code = 'DATABASE'
        elif isinstance(error, ImportError):
            code = 'DEPENDENCY'
    return f'[ACCOUNTS_STARTUP_{code}] {_MESSAGES[code]}'
