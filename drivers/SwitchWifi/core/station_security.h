/* SPDX-License-Identifier: ISC */
#ifndef SWIFI_STATION_SECURITY_H
#define SWIFI_STATION_SECURITY_H
#include <stddef.h>
#include <stdint.h>

/* BCM4356 brcmfmac fixed little-endian wire formats, infrastructure STA. */
enum { SWIFI_JOIN_LEN = 70, SWIFI_KEY_LEN = 164, SWIFI_RSN_MAX = 40 };
/* Both PSK modes use CCMP for unicast; mixed WPA/WPA2 APs may use a TKIP GTK. */
typedef enum {
    SWIFI_AUTH_OPEN = 1, SWIFI_AUTH_PSK_CCMP = 2, SWIFI_AUTH_PSK_CCMP_TKIP = 4
} swifi_auth;
typedef enum { SWIFI_CIPHER_NONE = 0, SWIFI_CIPHER_TKIP = 2, SWIFI_CIPHER_CCMP = 4 } swifi_cipher;
typedef struct swifi_bss_profile {
    uint8_t ssid[32], ssid_len;
    swifi_auth auth;
} swifi_bss_profile;

enum {
    SWIFI_BSS_FRAME = 1u, SWIFI_BSS_SSID = 2u, SWIFI_BSS_MODE = 4u,
    SWIFI_BSS_RSN_FORMAT = 8u, SWIFI_BSS_GROUP_CIPHER = 16u,
    SWIFI_BSS_PAIR_CIPHER = 32u, SWIFI_BSS_AKM = 64u,
    SWIFI_BSS_MFP_REQUIRED = 128u, SWIFI_BSS_MISSING_RSN = 256u,
    SWIFI_BSS_AUTH = 512u
};

/* Match a full beacon/probe-response body against one requested SSID and
 * an allowed authentication policy. A hidden SSID requires an explicit
 * host request. An AP advertising privacy can never match the open suite. */
int swifi_match_bss(const uint8_t *body, size_t length, const uint8_t *ssid, size_t ssid_len,
    int hidden, unsigned allowed_auth, swifi_bss_profile *profile);
/* Zero means a match. Failure bits contain no frame contents or identity. */
unsigned swifi_match_bss_reason(const uint8_t *body, size_t length, const uint8_t *ssid,
    size_t ssid_len, int hidden, unsigned allowed_auth, swifi_bss_profile *profile);
swifi_cipher swifi_group_cipher(swifi_auth auth);
size_t swifi_rsn_request(uint8_t out[SWIFI_RSN_MAX], swifi_auth auth, const uint8_t *pmkid);
/* Verify the negotiated request IEs against the selected Personal/open
 * suite and an optional PMKID. SSID is also checked via WLC_GET_SSID. */
int swifi_assoc_ies_match(const uint8_t *ies, size_t length,
    const swifi_bss_profile *profile, const uint8_t *pmkid);
int swifi_join_request(uint8_t out[SWIFI_JOIN_LEN], const swifi_bss_profile *profile,
    const uint8_t bssid[6], uint16_t chanspec);
/* pairwise keys have an explicit station peer; default group slots use a
 * null address, even when Windows identifies the AP in its group request. */
/* TKIP material is TK[16], RX MIC[8], TX MIC[8], as supplied by Windows. */
int swifi_cipher_key(uint8_t out[SWIFI_KEY_LEN], unsigned id, const uint8_t *peer,
    swifi_cipher cipher, const uint8_t *key, size_t key_length, const uint8_t *receive_pn);

/* Windows exemption values: no exemption (0), always (1), key unavailable (2). */
int swifi_clear_eapol_allowed(unsigned rule, int pairwise, int exclude_unencrypted);
/* Once this association has seen a TK, an idempotent retry keeps hardware
 * counters intact. A higher requested floor cannot be applied by reinstall. */
int swifi_keep_key_on_retry(int same_slot, int present,
    const uint8_t requested_pn[6], const uint8_t initial_pn[6]);

/* Held under the station protocol lock. The queue retains its current head
 * until it is consumed or cancelled. A full TX pool can defer classification. */
typedef enum swifi_tx_kind {
    SWIFI_TX_DATA, SWIFI_TX_UNCLASSIFIED, SWIFI_TX_EAPOL
} swifi_tx_kind;
typedef struct swifi_eapol_tx {
    unsigned pending;
    swifi_tx_kind head;
    int changing;
} swifi_eapol_tx;
int swifi_eapol_admit(swifi_eapol_tx *tx, swifi_tx_kind kind);
int swifi_eapol_idle(const swifi_eapol_tx *tx);

/* Validate frame-body IE lengths before translating firmware association
 * reports or including a candidate frame in a WDI indication. */
int swifi_valid_ies(const uint8_t *ies, size_t length);
#endif
