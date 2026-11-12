/* SPDX-License-Identifier: ISC */
#include "station_security.h"
#include <string.h>

static unsigned le16(const uint8_t *p) { return (unsigned)p[0] | (unsigned)p[1] << 8; }
static void put32(uint8_t *p, uint32_t n)
{
    for (unsigned i = 0; i < 4; ++i) p[i] = (uint8_t)(n >> (8*i));
}

int swifi_valid_ies(const uint8_t *p, size_t n)
{
    while (n) {
        if (!p || n < 2 || (size_t)p[1] + 2 > n) return 0;
        size_t size = (size_t)p[1] + 2;
        p += size;
        n -= size;
    }
    return 1;
}

static int has_suite(const uint8_t **cursor, size_t *left, uint8_t type)
{
    if (*left < 2) return -1;
    const uint8_t *p = *cursor;
    unsigned count = le16(p);
    if (!count || count > (*left - 2) / 4) return -1;
    int found = 0;
    for (unsigned i = 0; i < count; ++i) {
        const uint8_t *s = p + 2 + i*4;
        if (s[0] == 0 && s[1] == 0x0f && s[2] == 0xac && s[3] == type) found = 1;
    }
    size_t used = 2 + count*4;
    *cursor += used;
    *left -= used;
    return found;
}

static unsigned psk_ccmp_reason(const uint8_t *p, size_t n, swifi_cipher *group)
{
    if (n < 6 || le16(p) != 1) return SWIFI_BSS_RSN_FORMAT;
    if (memcmp(p+2, "\x00\x0f\xac", 3) || (p[5] != SWIFI_CIPHER_CCMP && p[5] != SWIFI_CIPHER_TKIP))
        return SWIFI_BSS_GROUP_CIPHER;
    *group = (swifi_cipher)p[5];
    p += 6; n -= 6;
    int suite = has_suite(&p, &n, 4);
    if (suite < 0) return SWIFI_BSS_RSN_FORMAT;
    if (!suite) return SWIFI_BSS_PAIR_CIPHER;
    suite = has_suite(&p, &n, 2);
    if (suite < 0) return SWIFI_BSS_RSN_FORMAT;
    if (!suite) return SWIFI_BSS_AKM;
    if (!n) return 0;
    if (n < 2) return SWIFI_BSS_RSN_FORMAT;
    if (le16(p) & 0x40) return SWIFI_BSS_MFP_REQUIRED;
    p += 2; n -= 2;
    if (!n) return 0;
    if (n < 2) return SWIFI_BSS_RSN_FORMAT;
    unsigned pmkids = le16(p);
    if (pmkids > (n - 2) / 16) return SWIFI_BSS_RSN_FORMAT;
    n -= 2 + pmkids*16;
    return n == 0 || n == 4 ? 0 : SWIFI_BSS_RSN_FORMAT;
}

unsigned swifi_match_bss_reason(const uint8_t *body, size_t length, const uint8_t *ssid, size_t ssid_len,
    int hidden, unsigned allowed, swifi_bss_profile *profile)
{
    if (!body || !ssid || !ssid_len || ssid_len > 32 || !profile || length < 12 ||
        !swifi_valid_ies(body+12, length-12)) return SWIFI_BSS_FRAME;
    unsigned caps = le16(body+10);
    if ((caps & 3) != 1) return SWIFI_BSS_MODE; /* ESS infrastructure only */
    int have_ssid = 0, rsn = 0, wpa = 0;
    swifi_cipher group = SWIFI_CIPHER_NONE;
    for (size_t i = 12; i < length;) {
        unsigned tag = body[i], n = body[i+1];
        const uint8_t *v = body+i+2;
        if (tag == 0) {
            if (have_ssid++ || n > 32) return SWIFI_BSS_SSID;
            size_t j = 0;
            while (j < n && !v[j]) ++j;
            if (j == n) { if (!hidden) return SWIFI_BSS_SSID; }
            else if (n != ssid_len || memcmp(v, ssid, n)) return SWIFI_BSS_SSID;
        } else if (tag == 48) {
            if (rsn) return SWIFI_BSS_RSN_FORMAT;
            unsigned reason = psk_ccmp_reason(v,n,&group);
            if (reason) return reason;
            rsn = 1;
        } else if (tag == 221 && n >= 4 && !memcmp(v,"\x00\x50\xf2\x01",4)) wpa = 1;
        i += 2+n;
    }
    if (!have_ssid) return SWIFI_BSS_SSID;
    if (!(caps & 0x10) && (rsn || wpa)) return SWIFI_BSS_AUTH;
    swifi_auth auth = !(caps & 0x10) ? SWIFI_AUTH_OPEN :
        group == SWIFI_CIPHER_TKIP ? SWIFI_AUTH_PSK_CCMP_TKIP : SWIFI_AUTH_PSK_CCMP;
    if (!(allowed & auth)) return SWIFI_BSS_AUTH;
    if (auth == SWIFI_AUTH_PSK_CCMP && !rsn) return SWIFI_BSS_MISSING_RSN;
    memset(profile, 0, sizeof(*profile));
    memcpy(profile->ssid, ssid, ssid_len);
    profile->ssid_len = (uint8_t)ssid_len;
    profile->auth = auth;
    return 0;
}

int swifi_match_bss(const uint8_t *body, size_t length, const uint8_t *ssid, size_t ssid_len,
    int hidden, unsigned allowed, swifi_bss_profile *profile)
{
    return !swifi_match_bss_reason(body,length,ssid,ssid_len,hidden,allowed,profile);
}

swifi_cipher swifi_group_cipher(swifi_auth auth)
{
    return auth == SWIFI_AUTH_PSK_CCMP ? SWIFI_CIPHER_CCMP :
        auth == SWIFI_AUTH_PSK_CCMP_TKIP ? SWIFI_CIPHER_TKIP : SWIFI_CIPHER_NONE;
}

size_t swifi_rsn_request(uint8_t out[SWIFI_RSN_MAX], swifi_auth auth, const uint8_t *pmkid)
{
    static const uint8_t rsn[] = {48,20,1,0,0,15,172,4,1,0,0,15,172,4,1,0,0,15,172,2,0,0};
    swifi_cipher group = swifi_group_cipher(auth);
    if (!out || !group) return 0;
    memcpy(out, rsn, sizeof(rsn));
    out[7] = (uint8_t)group;
    if (!pmkid) return sizeof(rsn);
    out[1] += 18;
    out[22] = 1; out[23] = 0;
    memcpy(out+24, pmkid, 16);
    return 40;
}

int swifi_assoc_ies_match(const uint8_t *ies, size_t length, const swifi_bss_profile *profile,
    const uint8_t *pmkid)
{
    if (!profile || !swifi_valid_ies(ies,length)) return 0;
    uint8_t expected[] = {1,0,0,15,172,4,1,0,0,15,172,4,1,0,0,15,172,2};
    expected[5] = (uint8_t)swifi_group_cipher(profile->auth);
    int rsn = 0, ssid = 0;
    for (size_t i = 0; i < length;) {
        const uint8_t *v = ies+i+2;
        unsigned tag = ies[i], n = ies[i+1];
        if (tag == 0 && (ssid++ || n != profile->ssid_len || memcmp(v,profile->ssid,n))) return 0;
        if (tag == 221 && n >= 4 && !memcmp(v,"\x00\x50\xf2\x01",4)) return 0;
        if (tag == 48) {
            swifi_cipher group;
            if (rsn++ || n < sizeof(expected) || memcmp(v,expected,sizeof(expected)) ||
                psk_ccmp_reason(v,n,&group)) return 0;
            if (pmkid) {
                if (n < 38 || le16(v+20) != 1 || memcmp(v+22,pmkid,16)) return 0;
            }
        }
        i += 2+n;
    }
    return profile->auth != SWIFI_AUTH_OPEN ? rsn == 1 : !rsn;
}

int swifi_join_request(uint8_t out[SWIFI_JOIN_LEN], const swifi_bss_profile *profile,
    const uint8_t bssid[6], uint16_t chanspec)
{
    if (!out || !profile || !profile->ssid_len || profile->ssid_len > 32 || !bssid ||
        (bssid[0] & 1) || !memcmp(bssid,"\0\0\0\0\0\0",6) || !chanspec) return 0;
    memset(out, 0, SWIFI_JOIN_LEN);
    put32(out, profile->ssid_len);
    memcpy(out+4, profile->ssid, profile->ssid_len);
    out[36] = 255;
    put32(out+40, 16); put32(out+44, 320); put32(out+48, 400); put32(out+52, UINT32_MAX);
    memcpy(out+56, bssid, 6);
    put32(out+64, 1);
    out[68] = (uint8_t)chanspec; out[69] = (uint8_t)(chanspec >> 8);
    return 1;
}

int swifi_cipher_key(uint8_t out[SWIFI_KEY_LEN], unsigned id, const uint8_t *peer,
    swifi_cipher cipher, const uint8_t *key, size_t key_length, const uint8_t *pn)
{
    if (!out || id > 3 || (peer && ((peer[0] & 1) || !memcmp(peer,"\0\0\0\0\0\0",6))) ||
        (cipher != SWIFI_CIPHER_CCMP && cipher != SWIFI_CIPHER_TKIP) ||
        (peer && cipher == SWIFI_CIPHER_TKIP) || (!key && (pn || key_length)) ||
        (key && key_length != (cipher == SWIFI_CIPHER_TKIP ? 32u : 16u))) return 0;
    memset(out, 0, SWIFI_KEY_LEN);
    put32(out, id);
    if (peer) memcpy(out+156, peer, 6);
    else put32(out+116, 2); /* BRCMF_PRIMARY_KEY */
    if (key) {
        put32(out+4, (uint32_t)key_length);
        /* brcmfmac swaps Linux's TX/RX MIC halves in station mode. Windows
         * already supplies RX/TX order, so swapping it again is incorrect. */
        memcpy(out+8, key, key_length);
        put32(out+112, (uint32_t)cipher); /* CRYPTO_ALGO_TKIP / AES_CCM */
        if (pn) {
            put32(out+132, 1);
            memcpy(out+140, pn+2, 4);
            memcpy(out+144, pn, 2);
        }
    }
    return 1;
}

int swifi_clear_eapol_allowed(unsigned rule, int pairwise, int exclude_unencrypted)
{
    if (rule == 1) return 1;
    if (rule == 2) return !pairwise;
    return rule == 0 && !exclude_unencrypted;
}

int swifi_eapol_admit(swifi_eapol_tx *tx, swifi_tx_kind kind)
{
    /* Once changing is set, only the head seen before the key request may
     * still contribute an EAPOL to the old key. No later head can enter. */
    if (tx->changing && tx->head == SWIFI_TX_DATA) return 0;
    if (kind == SWIFI_TX_UNCLASSIFIED) {
        if (tx->head == SWIFI_TX_DATA) tx->head = kind;
        return 0;
    }
    tx->head = kind;
    return !tx->changing || kind == SWIFI_TX_EAPOL;
}

int swifi_eapol_idle(const swifi_eapol_tx *tx)
{
    return tx->pending == 0 && tx->head == SWIFI_TX_DATA;
}

int swifi_keep_key_on_retry(int same_slot, int present,
    const uint8_t requested_pn[6], const uint8_t initial_pn[6])
{
    if (!same_slot || !present) return 0;
    for (int i = 5; i >= 0; --i)
        if (requested_pn[i] != initial_pn[i])
            return requested_pn[i] < initial_pn[i];
    return 1;
}
