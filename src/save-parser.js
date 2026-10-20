/**
 * Client-side parser for Yume 2kki / RPG Maker 2000-2003 LcfSaveData (.lsd).
 * Extracts inventory (effects), switches, variables, and a few visit hints.
 */

function readBer(view, offsetObj) {
    let result = 0;
    while (true) {
        if (offsetObj.o >= view.byteLength) throw new Error('EOF while reading BER');
        const b = view.getUint8(offsetObj.o++);
        result = (result << 7) | (b & 0x7f);
        if (!(b & 0x80)) break;
    }
    return result;
}

function readChunks(buffer) {
    const view = new DataView(buffer);
    const off = { o: 0 };
    const hlen = readBer(view, off);
    const headerBytes = new Uint8Array(buffer, off.o, hlen);
    off.o += hlen;
    const header = String.fromCharCode(...headerBytes);
    if (header !== 'LcfSaveData') {
        throw new Error('Not a valid LcfSaveData file');
    }

    const chunks = {};
    while (off.o < view.byteLength) {
        try {
            const cid = readBer(view, off);
            const csize = readBer(view, off);
            chunks[cid] = buffer.slice(off.o, off.o + csize);
            off.o += csize;
        } catch {
            break;
        }
    }
    return chunks;
}

function readSubchunks(buffer) {
    const view = new DataView(buffer);
    const off = { o: 0 };
    const subs = {};
    while (off.o < view.byteLength) {
        try {
            const sid = readBer(view, off);
            const ssize = readBer(view, off);
            subs[sid] = buffer.slice(off.o, off.o + ssize);
            off.o += ssize;
        } catch {
            break;
        }
    }
    return subs;
}

/**
 * Typical Yume 2kki effect item IDs (RPG Maker database).
 * ordinal 0 = Bike … 34 = Dice, Instructions often item 50.
 * Mapping can shift between major versions; treat as best-effort.
 */
const EFFECT_ITEM_TO_ORDINAL = {
    50: 35, // Instructions (説明書) — often first or dedicated slot
    51: 0,  // Bike
    52: 1,  // Boy
    53: 2,  // Chainsaw
    54: 3,  // Lantern
    55: 4,  // Fairy
    56: 5,  // Spacesuit
    57: 6,  // Glasses
    58: 7,  // Rainbow
    59: 8,  // Wolf
    60: 9,  // Eyeball Bomb
    61: 10, // Telephone
    62: 11, // Maiko
    63: 12, // Twintails
    64: 13, // Penguin
    65: 14, // Insect
    66: 15, // Spring
    67: 16, // Invisible
    68: 17, // Plaster Cast / Gakuran slot varies
    69: 18,
    70: 19,
    71: 20,
    72: 21,
    73: 22,
    74: 23,
    75: 24,
    76: 25,
    77: 26,
    78: 27,
    79: 28,
    80: 29,
    81: 30,
    82: 31,
    83: 32,
    84: 33,
    85: 34, // Dice
    86: 35,
    87: 35,
};

/** Documented visit-count variables → English world title substrings for matching */
const VISIT_VAR_TO_TITLE = {
    162: ["Urotsuki's Room", "Urotsuki Room"],
    163: ["Balcony"],
    164: ["Urotsuki's Room", "Dream Room"],
    165: ["Balcony"],
    166: ["Sound Room"],
    170: ["Nexus", "Door Room"],
    171: ["Hand Hub"],
    172: ["Chocolate World"],
    173: ["Art Gallery", "Gallery"],
    174: ["Red Streetlight"],
    175: ["Theatre World", "Theater"],
    176: ["Broken Faces"],
    177: ["Intestines Maze", "Intestine"],
    178: ["Gray Road", "Grey Road"],
};

function parseInventory(statusBuf) {
    const subs = readSubchunks(statusBuf);
    const inv = {};
    if (!subs[12] || !subs[13]) return inv;
    const idsView = new DataView(subs[12]);
    const qtys = new Uint8Array(subs[13]);
    const n = Math.min(idsView.byteLength / 2, qtys.length);
    for (let i = 0; i < n; i++) {
        const id = idsView.getUint16(i * 2, true);
        const q = qtys[i];
        if (q > 0) inv[id] = q;
    }
    return inv;
}

function parseSwitches(systemBuf) {
    const subs = readSubchunks(systemBuf);
    const data = subs[0x20] ? new Uint8Array(subs[0x20]) : new Uint8Array(0);
    const on = [];
    for (let i = 0; i < data.length; i++) {
        if (data[i]) on.push(i + 1);
    }
    return on;
}

function parseVariables(systemBuf) {
    const subs = readSubchunks(systemBuf);
    const data = subs[0x22] ? new DataView(subs[0x22]) : null;
    const out = {};
    if (!data) return out;
    const n = Math.floor(data.byteLength / 4);
    for (let i = 0; i < n; i++) {
        const v = data.getInt32(i * 4, true);
        if (v !== 0) out[i + 1] = v;
    }
    return out;
}

/**
 * @param {ArrayBuffer} buffer
 * @returns {{ effects: { itemId: number, ordinal: number|null, quantity: number }[],
 *             effectOrdinals: Set<number>,
 *             switchesOn: number[],
 *             variables: Record<number, number>,
 *             visitedTitleHints: string[],
 *             collectedEffectCount: number }}
 */
export function parseLsdSave(buffer) {
    const chunks = readChunks(buffer);
    const system = chunks[101];
    const status = chunks[109];

    const inventory = status ? parseInventory(status) : {};
    const switchesOn = system ? parseSwitches(system) : [];
    const variables = system ? parseVariables(system) : {};

    const effects = [];
    const effectOrdinals = new Set();
    for (const [idStr, qty] of Object.entries(inventory)) {
        const itemId = +idStr;
        const ordinal = EFFECT_ITEM_TO_ORDINAL[itemId] ?? null;
        effects.push({ itemId, ordinal, quantity: qty });
        if (ordinal != null) effectOrdinals.add(ordinal);
    }

    // Also accept contiguous item range 51–85 as ordinals 0–34 if mapping misses
    if (effectOrdinals.size < 5) {
        for (const [idStr, qty] of Object.entries(inventory)) {
            const itemId = +idStr;
            if (itemId >= 51 && itemId <= 85) {
                effectOrdinals.add(itemId - 51);
            }
        }
    }

    const visitedTitleHints = [];
    for (const [vid, titles] of Object.entries(VISIT_VAR_TO_TITLE)) {
        if ((variables[+vid] || 0) > 0) {
            visitedTitleHints.push(...titles);
        }
    }

    return {
        effects,
        effectOrdinals,
        switchesOn,
        variables,
        visitedTitleHints: [...new Set(visitedTitleHints)],
        collectedEffectCount: effectOrdinals.size,
        inventory,
    };
}

/**
 * Match world titles against visit hints (case-insensitive substring).
 * @param {Array<{id:number, title?:string, titleJP?:string}>} worldData
 * @param {string[]} titleHints
 * @returns {Set<number>} world ids
 */
export function matchVisitedWorlds(worldData, titleHints) {
    const ids = new Set();
    if (!titleHints.length || !worldData) return ids;
    const hints = titleHints.map(h => h.toLowerCase());
    for (const w of worldData) {
        const t = (w.title || '').toLowerCase();
        const tjp = (w.titleJP || '').toLowerCase();
        if (hints.some(h => t.includes(h) || tjp.includes(h))) {
            ids.add(w.id);
        }
    }
    return ids;
}
