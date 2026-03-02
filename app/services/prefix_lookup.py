"""Prefix lookup — maps MPN prefixes to canonical manufacturer names.

Sorted longest-first for most-specific matching. Returns (manufacturer, confidence)
where confidence is 0.9 for prefixes >= 3 chars and 0.7 for 2-char prefixes.

Called by: app.services.tagging (classify_material_card)
Depends on: nothing (pure data + logic)
"""

from loguru import logger

# Prefix → canonical manufacturer name
# Organized by manufacturer; longest prefixes first within each group.
PREFIX_TABLE: dict[str, str] = {
    # Texas Instruments
    "TPS": "Texas Instruments",
    "TMS": "Texas Instruments",
    "TLV": "Texas Instruments",
    "OPA": "Texas Instruments",
    "ADS": "Texas Instruments",
    "INA": "Texas Instruments",
    "MSP": "Texas Instruments",
    "TCA": "Texas Instruments",
    "DRV": "Texas Instruments",
    "UCC": "Texas Instruments",
    "TPA": "Texas Instruments",
    "LMR": "Texas Instruments",
    "SN7": "Texas Instruments",
    "SN6": "Texas Instruments",
    "SN": "Texas Instruments",
    "LM": "Texas Instruments",
    "BQ": "Texas Instruments",
    # NXP Semiconductors
    "LPC": "NXP Semiconductors",
    "S32K": "NXP Semiconductors",
    "MFRC": "NXP Semiconductors",
    "PN5": "NXP Semiconductors",
    "TJA": "NXP Semiconductors",
    "PCA": "NXP Semiconductors",
    "PCF": "NXP Semiconductors",
    "MK": "NXP Semiconductors",
    # STMicroelectronics
    "STM32": "STMicroelectronics",
    "STM8": "STMicroelectronics",
    "STM": "STMicroelectronics",
    "LIS": "STMicroelectronics",
    "LSM": "STMicroelectronics",
    "VNH": "STMicroelectronics",
    "STP": "STMicroelectronics",
    "L78": "STMicroelectronics",
    "L79": "STMicroelectronics",
    "ST": "STMicroelectronics",
    # Analog Devices (ADI)
    "ADM": "Analog Devices",
    "ADP": "Analog Devices",
    "ADG": "Analog Devices",
    "LTC": "Analog Devices",
    "LTM": "Analog Devices",
    "MAX": "Analog Devices",
    "LT": "Analog Devices",
    "DS": "Analog Devices",
    "AD": "Analog Devices",
    # Microchip Technology
    "ATMEGA": "Microchip Technology",
    "ATTINY": "Microchip Technology",
    "ATSAMD": "Microchip Technology",
    "ATSAM": "Microchip Technology",
    "dsPIC": "Microchip Technology",
    "PIC32": "Microchip Technology",
    "PIC24": "Microchip Technology",
    "PIC18": "Microchip Technology",
    "PIC16": "Microchip Technology",
    "PIC12": "Microchip Technology",
    "PIC10": "Microchip Technology",
    "AT24": "Microchip Technology",
    "AT25": "Microchip Technology",
    "MCP": "Microchip Technology",
    "PIC": "Microchip Technology",
    # Intel
    "XEON": "Intel",
    # AMD
    "EPYC": "AMD",
    "RYZEN": "AMD",
    # Infineon Technologies
    "IRFP": "Infineon Technologies",
    "AUIR": "Infineon Technologies",
    "IRF": "Infineon Technologies",
    "IFX": "Infineon Technologies",
    "BSC": "Infineon Technologies",
    "TLE": "Infineon Technologies",
    "XMC": "Infineon Technologies",
    "CY": "Infineon Technologies",
    # onsemi
    "MMBT": "onsemi",
    "NCP": "onsemi",
    "FQP": "onsemi",
    "NCV": "onsemi",
    "NTD": "onsemi",
    "NSI": "onsemi",
    "NJM": "onsemi",
    "MC": "onsemi",
    # Renesas Electronics
    "R5F": "Renesas Electronics",
    "R7F": "Renesas Electronics",
    "RL78": "Renesas Electronics",
    "UPD": "Renesas Electronics",
    "ISL": "Renesas Electronics",
    "RX": "Renesas Electronics",
    "ZL": "Renesas Electronics",
    # Samsung
    "K4": "Samsung",
    "K9": "Samsung",
    # Micron Technology
    "M25P": "Micron Technology",
    "MT": "Micron Technology",
    # Vishay
    "CRCW": "Vishay",
    "TNPW": "Vishay",
    "MRS": "Vishay",
    # Murata
    "GRM": "Murata",
    "BLM": "Murata",
    "LQH": "Murata",
    "NFM": "Murata",
    # TDK
    "MLF": "TDK",
    "MPZ": "TDK",
    "ACM": "TDK",
    # Xilinx (AMD)
    "XCVU": "Xilinx",
    "XCKU": "Xilinx",
    "XC": "Xilinx",
    # Altera (Intel)
    "10M": "Altera",
    "EP": "Altera",
    "5C": "Altera",
    # ISSI
    "IS42": "ISSI",
    "IS62": "ISSI",
    # Winbond
    "W9825": "Winbond",
    "W25": "Winbond",
    # Broadcom / Avago
    "ACPL": "Broadcom",
    "HCPL": "Broadcom",
    "BCM": "Broadcom",
    # Qualcomm
    "QCA": "Qualcomm",
    "MDM": "Qualcomm",
    "WCN": "Qualcomm",
    # Nordic Semiconductor
    "NRF": "Nordic Semiconductor",
    # Espressif Systems
    "ESP": "Espressif Systems",
    # FTDI
    "FT": "FTDI",
    # Silicon Labs
    "EFR": "Silicon Labs",
    "EFM": "Silicon Labs",
    "SI": "Silicon Labs",
    # MediaTek
    "MT6": "MediaTek",
    "MT7": "MediaTek",
    # Lattice Semiconductor
    "LCMXO": "Lattice Semiconductor",
    "ICE40": "Lattice Semiconductor",
    # Skyworks Solutions
    "SKY": "Skyworks Solutions",
    # Marvell
    "88E": "Marvell",
    # Infineon (PSoC / Cypress)
    "CY8C": "Infineon Technologies",
    "PSOC": "Infineon Technologies",
    # Allegro MicroSystems
    "ACS": "Allegro MicroSystems",
    "A49": "Allegro MicroSystems",
    # Bosch Sensortec
    "BME": "Bosch Sensortec",
    "BNO": "Bosch Sensortec",
    "BMP": "Bosch Sensortec",
    # ams-OSRAM
    "TMD": "ams-OSRAM",
    "AS7": "ams-OSRAM",
    # NXP (legacy TDA series)
    "TDA": "NXP Semiconductors",
    # ROHM Semiconductor
    "BD": "ROHM Semiconductor",
    "BR": "ROHM Semiconductor",
    # Nexperia
    "PMBT": "Nexperia",
    "BAT": "Nexperia",
    "BAS": "Nexperia",
    # Toshiba
    "TLP": "Toshiba",
    "TC": "Toshiba",
    # Panasonic
    "EEE": "Panasonic",
    "ERJ": "Panasonic",
    # KEMET
    "C0805": "KEMET",
    "T491": "KEMET",
    # Yageo
    "RC": "Yageo",
    "CC": "Yageo",
    # TE Connectivity
    "AMP": "TE Connectivity",
    # Molex
    "MOLEX": "Molex",
    # Amphenol
    "AMPHENOL": "Amphenol",
    # Samtec
    "SAMTEC": "Samtec",
    # Hirose
    "DF": "Hirose",
    # Wurth Elektronik
    "WE-": "Wurth Elektronik",
}

# Pre-sorted longest-first for most-specific matching
_SORTED_PREFIXES: list[tuple[str, str]] = sorted(
    PREFIX_TABLE.items(), key=lambda x: len(x[0]), reverse=True
)


def lookup_manufacturer_by_prefix(normalized_mpn: str) -> tuple[str | None, float]:
    """Look up manufacturer by MPN prefix.

    Args:
        normalized_mpn: Lowercase MPN string.

    Returns:
        (manufacturer_name, confidence) or (None, 0.0) if no match.
        Confidence is 0.9 for prefixes >= 3 chars, 0.7 for 2-char prefixes.
    """
    upper_mpn = normalized_mpn.upper()
    for prefix, manufacturer in _SORTED_PREFIXES:
        if upper_mpn.startswith(prefix.upper()):
            confidence = 0.9 if len(prefix) >= 3 else 0.7
            logger.debug(f"Prefix match: {normalized_mpn!r} → {manufacturer} (prefix={prefix!r}, conf={confidence})")
            return manufacturer, confidence
    return None, 0.0
