#!/usr/bin/env python3
"""
CYTHANX MOG — STRATEGIC FUSION ENGINE
===================

One program with explicit cryptographic layers:

    source bytes
        -> SHA256
        -> Enigma-inspired transform
        -> BLAKE3-256 fusion root
        -> SHA256d fusion state
        -> CYTHANIZE / Scrypt commitment
        -> JOATT state
        -> optional blockchain header
        -> Scrypt
        -> SHA256d final block hash

The miner is native to the CYTHANX layered proof-of-work engine.

No wallet, password, token, or pool credential is embedded.
"""

from __future__ import annotations
import argparse, hashlib, hmac, json, math, os, secrets, signal, subprocess, threading, time, unicodedata, urllib.request, urllib.error
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from blake3 import blake3 as _blake3
except ImportError:
    _blake3 = None


# ---------------------------------------------------------------------------
# CORE HASH LAYERS
# ---------------------------------------------------------------------------

def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

def cyx_root(core: bytes) -> bytes:
    """Domain-separated CYX root function for the canonical core string."""
    return blake3_256(b"CYTHANX|ROOT|" + core)

def cyx_core_string(*parts: bytes | str) -> tuple[bytes, bytes]:
    """Build one canonical CYX core string and jam its root into that string."""
    body = b"|".join(
        p if isinstance(p, bytes) else str(p).encode()
        for p in parts
    )
    root = cyx_root(body)
    core = body + b"|ROOT|" + root.hex().encode()
    return core, root

def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()

def sha256d(data: bytes) -> bytes:
    return sha256(sha256(data))

def blake3_256(data: bytes) -> bytes:
    if _blake3 is not None:
        return _blake3(data).digest(length=32)
    return hashlib.blake2b(b"CYTHANX|BLAKE3-FALLBACK|" + data, digest_size=32).digest()

def scrypt256(data: bytes, salt: bytes | None = None) -> bytes:
    return hashlib.scrypt(
        password=data, salt=data if salt is None else salt,
        n=1024, r=1, p=1, dklen=32
    )

def pbkdf2_sha512(data: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", data, salt, 2048, 64)


# ---------------------------------------------------------------------------
# STANDARDS-BASED DRAGON-ENIGMA LAYER
# ---------------------------------------------------------------------------
# Dragon/Enigma are CYTHANX protocol labels, not standardized algorithms.
# The cryptographic primitives used here are SHA-256, SHAKE256 and
# HKDF-HMAC-SHA256 (FIPS 180-4, FIPS 202, RFC 5869 respectively).

DRAGON_DOMAIN = b"CYTHANX|DRAGON|V1|"
ENIGMA_DOMAIN = b"CYTHANX|ENIGMA|V1|"
DRAGON_ENIGMA_DOMAIN = b"CYTHANX|DRAGON-ENIGMA|V1|"


def shake256(data: bytes, length: int) -> bytes:
    if length < 0:
        raise ValueError("SHAKE256 output length must be non-negative")
    return hashlib.shake_256(data).digest(length)


def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    if not salt:
        salt = b"\\x00" * 32
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    if length < 0 or length > 255 * 32:
        raise ValueError("invalid HKDF output length")
    out = bytearray()
    previous = b""
    for counter in range(1, (length + 31) // 32 + 1):
        previous = hmac.new(
            prk, previous + info + bytes([counter]), hashlib.sha256
        ).digest()
        out.extend(previous)
    return bytes(out[:length])


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    return hkdf_expand(hkdf_extract(salt, ikm), info, length)


def dragon_enigma_keys(master_key: bytes) -> tuple[bytes, bytes]:
    root = hkdf_sha256(
        master_key, DRAGON_DOMAIN, DRAGON_ENIGMA_DOMAIN, 64
    )
    return root[:32], root[32:]


def dragon_state(header: bytes, nonce: int, dragon_key: bytes) -> bytes:
    return sha256(
        DRAGON_DOMAIN + dragon_key + header + nonce.to_bytes(8, "little")
    )


def enigma_mix(state: bytes, enigma_key: bytes, output_length: int = 32) -> bytes:
    return shake256(ENIGMA_DOMAIN + enigma_key + state, output_length)


def dragon_enigma_digest(header: bytes, nonce: int, master_key: bytes) -> bytes:
    dragon_key, enigma_key = dragon_enigma_keys(master_key)
    state = dragon_state(header, nonce, dragon_key)
    mixed = enigma_mix(state, enigma_key, 32)
    return sha256(DRAGON_ENIGMA_DOMAIN + state + mixed)


def dragon_trend(digest: bytes) -> float:
    if len(digest) < 16:
        raise ValueError("digest must contain at least 16 bytes")
    left = int.from_bytes(digest[:8], "big")
    right = int.from_bytes(digest[8:16], "big")
    return (left ^ right) / ((1 << 64) - 1)


def dragon_phase(trend: float) -> str:
    if trend < 0.20:
        return "QUIET"
    if trend < 0.40:
        return "RISING"
    if trend < 0.60:
        return "OSCILLATING"
    if trend < 0.80:
        return "TURBULENT"
    return "DRAGON"


def dragon_lower_gate(digest: bytes, lower_bits: int = 8) -> bool:
    if not 0 <= lower_bits <= len(digest) * 8:
        raise ValueError("dragon lower gate bits out of range")
    if lower_bits == 0:
        return True
    value = int.from_bytes(digest, "big")
    return (value & ((1 << lower_bits) - 1)) == 0

def joaat(data: bytes) -> int:
    h = 0
    for byte in data:
        h = (h + byte) & 0xffffffff
        h = (h + ((h << 10) & 0xffffffff)) & 0xffffffff
        h ^= h >> 6
    h = (h + ((h << 3) & 0xffffffff)) & 0xffffffff
    h ^= h >> 11
    h = (h + ((h << 15) & 0xffffffff)) & 0xffffffff
    return h & 0xffffffff


# ---------------------------------------------------------------------------
# ENIGMA-INSPIRED AUXILIARY TRANSFORM
# ---------------------------------------------------------------------------

def _rotl8(x: int, n: int) -> int:
    n &= 7
    return ((x << n) | (x >> (8 - n))) & 0xff

def enigma_bytes(data: bytes, key: bytes) -> bytes:
    if len(key) < 32:
        raise ValueError("Enigma key must be at least 32 bytes")
    out = bytearray(len(data))
    for block_start in range(0, len(data), 32):
        block = data[block_start:block_start + 32]
        counter = block_start // 32
        stream = sha256(b"CYTHANX|ENIGMA|STREAM|" + key + counter.to_bytes(8, "big"))
        for j, byte in enumerate(block):
            k = stream[j]
            x = (byte + k + ((block_start + j) * 17)) & 0xff
            x ^= (key[(counter + j) % len(key)] + j * 29) & 0xff
            x = _rotl8(x, (k ^ key[j % len(key)]) & 7)
            out[block_start + j] = x
    return bytes(out)


# ---------------------------------------------------------------------------
# CYTHAN / JOATT LAYERS
# ---------------------------------------------------------------------------

VECTOR_KEY = b"CYTHAN-MAINNET-V1"
CHAIN_ID = "CYTHANX-MEGA-FUSION"

def cythan_vector() -> dict:
    vector = blake3_256(b"CYTHAN|VECTOR|" + CHAIN_ID.encode() + b"|" + VECTOR_KEY)
    return {"vector": vector, "joaat": joaat(vector)}

def cythanize(payload: bytes, previous: bytes = b"\0" * 32) -> dict:
    v = cythan_vector()
    vector, j = v["vector"], v["joaat"]
    jb = j.to_bytes(4, "big")
    state = blake3_256(b"CYTHAN|STATE|" + payload + previous + vector + jb)
    sha_state = sha256d(b"CYTHAN|SHA256D|" + state)
    scrypt_state = scrypt256(b"CYTHAN|SCRYPT|" + sha_state)
    commitment = blake3_256(
        b"CYTHAN|COMMITMENT|" + vector + jb + state + sha_state + scrypt_state
    )
    return {
        "vector": vector.hex(), "joaat": f"{j:08x}",
        "state": state.hex(), "sha256d": sha_state.hex(),
        "scrypt": scrypt_state.hex(), "commitment": commitment.hex()
    }

def joatt(payload: bytes, previous_state: bytes = b"\0" * 32) -> dict:
    b3_state = blake3_256(b"JOATT|BLAKE3|" + CHAIN_ID.encode() + previous_state + payload)
    root = sha256d(b"JOATT|ROOT|" + previous_state + b3_state + payload)
    pbkdf = pbkdf2_sha512(root, b"JOATT|PBKDF2|" + CHAIN_ID.encode())
    scrypt_state = scrypt256(pbkdf, b"JOATT|SCRYPT|" + CHAIN_ID.encode())
    mixed = blake3_256(b"JOATT|MIX|" + b3_state + root + pbkdf + scrypt_state)
    checksum = blake3_256(b"JOATT|CHECKSUM|" + mixed + root + scrypt_state)[:4]
    identifier64 = blake3_256(
        b"JOATT|ID64|" + checksum + mixed + previous_state
    )[:8]
    state256 = blake3_256(
        b"JOATT|STATE256|" + b3_state + root + pbkdf + scrypt_state +
        mixed + checksum + identifier64
    )
    return {
        "blake3": b3_state.hex(), "root_sha256d": root.hex(),
        "pbkdf2": pbkdf.hex(), "scrypt": scrypt_state.hex(),
        "mix": mixed.hex(), "checksum": checksum.hex(),
        "identifier64": identifier64.hex(), "state256": state256.hex()
    }

def transmogrify(payload: bytes, rounds: int = 3) -> bytes:
    x = payload
    for i in range(rounds):
        local_key = sha256(b"CYTHANX|LOCAL-ROTOR|" + i.to_bytes(4, "big"))
        x = enigma_bytes(x, local_key)
        x = blake3_256(b"CYTHANX|TRANSMOGRIFY|" + bytes([i]) + x)
        x = scrypt256(x, f"round-{i}".encode())
        x = sha256d(b"CYTHANX|SHA256D|ROUND|" + x)
    return x


# ---------------------------------------------------------------------------
# SOURCE FUSION
# ---------------------------------------------------------------------------

def digest_file(path: Path) -> dict:
    raw = path.read_bytes()
    return {"name": str(path), "bytes": len(raw), "sha256": sha256(raw).hex()}

def fuse_files(paths: list[Path], seed: str | None = None) -> dict:
    key = sha256(seed.encode()) if seed else secrets.token_bytes(32)
    chunks, records = [], []
    for path in paths:
        if not path.exists():
            records.append({"name": str(path), "status": "missing"})
            continue
        raw = path.read_bytes()
        transformed = enigma_bytes(raw, key)
        actual = sha256(raw)
        records.append({
            "name": str(path), "status": "ok", "bytes": len(raw),
            "sha256": actual.hex(), "enigma_sha256": sha256(transformed).hex()
        })
        chunks.append(path.name.encode() + b"\0" + len(raw).to_bytes(8, "big") + actual + transformed)
    envelope = b"CYTHANX|MEGA-FUSION|V2|" + b"".join(chunks)
    root = blake3_256(envelope)
    state = sha256d(b"CYTHANX|FUSION-SHA256D|" + root)
    commitment = blake3_256(b"CYTHANX|FUSION-COMMIT|" + root + state + key)
    return {
        "version": 2, "source_count": len(paths), "key_hex": key.hex(),
        "records": records, "fusion_root": root.hex(),
        "fusion_sha256d": state.hex(), "fusion_commitment": commitment.hex()
    }


# ---------------------------------------------------------------------------
# LAYERED BLOCK ENGINE
# ---------------------------------------------------------------------------

# Compact-difficulty encoding used by the canonical header.  The target is
# derived from nBits, so the field that is actually committed in the 80-byte
# header also controls proof-of-work validation.
DEFAULT_NBITS = 0x1E0FFFFF

def compact_to_target(nbits: int) -> int:
    if not 0 <= nbits <= 0xFFFFFFFF:
        raise ValueError("nBits must fit uint32")
    exponent = (nbits >> 24) & 0xFF
    mantissa = nbits & 0x007FFFFF
    if mantissa == 0:
        raise ValueError("nBits mantissa must be non-zero")
    if nbits & 0x00800000:
        raise ValueError("negative compact targets are not valid")
    if exponent <= 3:
        target = mantissa >> (8 * (3 - exponent))
    else:
        target = mantissa << (8 * (exponent - 3))
    if target <= 0 or target >= 1 << 256:
        raise ValueError("nBits decodes to an invalid 256-bit target")
    return target

TARGET = compact_to_target(DEFAULT_NBITS)

# ---------------------------------------------------------------------------
# PERSISTENT SHARE LEDGER / GENESIS / JOATT CONFORMANCE
# ---------------------------------------------------------------------------
# This ledger records CYTHANX work locally. It does not turn CYTHANX shares
# into Bitcoin shares or claim BTC payout. A Bitcoin share/block remains
# independently subject to Bitcoin's own header and SHA256d target rules.
LEDGER_VERSION = 1
LEDGER_DIR = Path("data/cythanx")
LEDGER_FILE = LEDGER_DIR / "share_ledger.jsonl"
GENESIS_DOMAIN = b"CYTHANX|GENESIS|V1|"
LEDGER_DOMAIN = b"CYTHANX|SHARE-LEDGER|V1|"
JOATT_DOMAIN = b"CYTHANX|JOATT-CONFORMANCE|V1|"
DRAGONFLY_DOMAIN = b"CYTHANX|DRAGONFLY|ORGANISM-OF-WORK|V1|"

def genesis_block() -> "Block":
    seed = sha256(GENESIS_DOMAIN)
    payload = cythanize(seed)["commitment"]
    return Block(0, 0, "00" * 32, payload)

def genesis_fingerprint() -> dict:
    block = genesis_block()
    header = block.header_bytes()
    layered = block.layered_hash()
    j = joatt(header)
    return {
        "height": block.height,
        "payload": block.payload,
        "header_hex": header.hex(),
        "header_sha256d": sha256d(header).hex(),
        "layered_hash": layered.hex(),
        "joatt_state256": j["state256"],
        "joatt_identifier64": j["identifier64"],
        "nbits": f"{block.nbits:08x}",
        "version": block.version,
    }

def verify_genesis(expected: dict | None = None) -> tuple[bool, dict, str]:
    actual = genesis_fingerprint()
    if expected is None:
        return True, actual, "deterministic genesis recomputation"
    ok = actual == expected
    return ok, actual, "genesis matches expected fingerprint" if ok else "genesis mismatch"

def _ledger_line_hash(record_without_hash: dict) -> str:
    return sha256d(LEDGER_DOMAIN + canonical(record_without_hash)).hex()

class ShareLedger:
    """Append-only, tamper-evident CYTHANX share ledger with JOATT chaining."""

    def __init__(self, path: Path = LEDGER_FILE):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_initialized()

    def _ensure_initialized(self) -> None:
        with self.lock:
            if self.path.exists() and self.path.stat().st_size:
                ok, _, reason = self.verify()
                if not ok:
                    raise RuntimeError(f"share ledger verification failed: {reason}")
                return
            genesis_ok, genesis, reason = verify_genesis()
            if not genesis_ok:
                raise RuntimeError(f"cannot initialize ledger: {reason}")
            record = {
                "record_type": "genesis",
                "ledger_version": LEDGER_VERSION,
                "timestamp": 0,
                "genesis": genesis,
                "previous_record_hash": "00" * 32,
                "joatt_previous_state256": "00" * 32,
                "joatt_state256": genesis["joatt_state256"],
                "joatt_identifier64": genesis["joatt_identifier64"],
            }
            record["record_hash"] = _ledger_line_hash(record)
            self._append_raw(record)

    def _append_raw(self, record: dict) -> None:
        line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def records(self) -> list[dict]:
        with self.lock:
            if not self.path.exists():
                return []
            out = []
            with self.path.open("r", encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(f"invalid ledger JSON at line {line_no}") from exc
            return out

    def verify(self) -> tuple[bool, dict, str]:
        with self.lock:
            try:
                records = self.records()
                if not records:
                    return False, {}, "ledger is empty"
                expected_genesis = genesis_fingerprint()
                first = records[0]
                if first.get("record_type") != "genesis" or first.get("genesis") != expected_genesis:
                    return False, {}, "genesis anchor mismatch"
                previous_hash = "00" * 32
                previous_joatt = "00" * 32
                shares = 0
                for idx, record in enumerate(records):
                    stored = record.get("record_hash")
                    body = dict(record)
                    body.pop("record_hash", None)
                    if stored != _ledger_line_hash(body):
                        return False, {"records": idx}, f"record hash mismatch at index {idx}"
                    if record.get("previous_record_hash") != previous_hash:
                        return False, {"records": idx}, f"record chain mismatch at index {idx}"
                    if record.get("joatt_previous_state256") != previous_joatt:
                        return False, {"records": idx}, f"JOATT chain mismatch at index {idx}"
                    previous_hash = stored
                    previous_joatt = record.get("joatt_state256", previous_joatt)
                    if record.get("record_type") == "share":
                        shares += 1
                return True, {
                    "records": len(records),
                    "shares": shares,
                    "head_record_hash": previous_hash,
                    "head_joatt_state256": previous_joatt,
                    "genesis": expected_genesis,
                }, "ledger verified"
            except Exception as exc:
                return False, {}, str(exc)

    def append_share(self, block: "Block", source: str = "local",
                     alien_string: str | None = None,
                     alien_gate_bits: int | None = None,
                     dragon_enigma_key: bytes | None = None,
                     dragon_gate_bits: int = 0) -> dict:
        """Record a locally accepted CYTHANX block/share after all conformance checks."""
        with self.lock:
            if alien_string is None:
                alien_string = ALIEN_GATE_STRING
            if alien_gate_bits is None:
                alien_gate_bits = ALIEN_GATE_BITS
            ok, state, reason = self.verify()
            if not ok:
                raise RuntimeError(f"cannot append share: {reason}")
            records = self.records()
            previous = records[-1]
            header = block.header_bytes()
            pow_digest = block.layered_hash()
            pow_target = compact_to_target(block.nbits)
            pow_pass = int.from_bytes(pow_digest, "big") <= pow_target
            alien_pass = alien_gate_passes(header, alien_string, alien_gate_bits)
            dragon_pass = True
            dragon_digest = None
            if dragon_enigma_key is not None:
                dragon_digest = dragon_enigma_digest(header, block.nonce, dragon_enigma_key)
                dragon_pass = dragon_lower_gate(dragon_digest, dragon_gate_bits)
            if not (pow_pass and alien_pass and dragon_pass):
                raise ValueError("share does not satisfy CYTHANX acceptance conditions")

            joatt_input = (
                JOATT_DOMAIN + header + pow_digest +
                bytes.fromhex(previous.get("joatt_state256", "00" * 32))
            )
            j = joatt(joatt_input, bytes.fromhex(previous.get("joatt_state256", "00" * 32)))
            record = {
                "record_type": "share",
                "ledger_version": LEDGER_VERSION,
                "timestamp": int(time.time()),
                "source": source,
                "height": block.height,
                "block_hash": pow_digest.hex(),
                "header_hex": header.hex(),
                "nonce": block.nonce,
                "nBits": f"{block.nbits:08x}",
                "alien_gate": {"string": alien_string, "bits": alien_gate_bits, "passed": alien_pass},
                "dragon_enigma": {
                    "enabled": dragon_enigma_key is not None,
                    "bits": dragon_gate_bits,
                    "digest": dragon_digest.hex() if dragon_digest else None,
                    "passed": dragon_pass,
                },
                "pow_target": f"{pow_target:064x}",
                "joatt_previous_state256": previous.get("joatt_state256", "00" * 32),
                "joatt_state256": j["state256"],
                "joatt_identifier64": j["identifier64"],
                "joatt_input_sha256d": sha256d(joatt_input).hex(),
                "previous_record_hash": previous["record_hash"],
            }
            record["record_hash"] = _ledger_line_hash(record)
            self._append_raw(record)
            return record

    def summary(self) -> dict:
        ok, state, reason = self.verify()
        return {"ok": ok, "reason": reason, **state, "path": str(self.path)}


# ---------------------------------------------------------------------------
# ALIEN STRING GATE
# ---------------------------------------------------------------------------
# The gate is a second, deterministic acceptance condition over the exact
# serialized 80-byte header.  It is consensus-style logic for this experimental
# chain: a candidate must satisfy BOTH the normal layered PoW target and this
# domain-separated gate.  The default 4-bit gate rejects roughly 15/16 of
# otherwise valid PoW candidates without changing the underlying PoW hash.
ALIEN_GATE_STRING = "CYTHANX|ALIEN|STRING|GATE|V1"
ALIEN_GATE_BITS = 4

def alien_gate_digest(header: bytes, gate_string: str = ALIEN_GATE_STRING) -> bytes:
    if len(header) != HEADER_SIZE:
        raise ValueError(f"alien gate requires an {HEADER_SIZE}-byte header")
    if not isinstance(gate_string, str) or not gate_string:
        raise ValueError("alien gate string must be a non-empty string")
    return sha256d(gate_string.encode("utf-8") + b"|" + header)

def alien_gate_passes(
    header: bytes,
    gate_string: str = ALIEN_GATE_STRING,
    gate_bits: int = ALIEN_GATE_BITS,
) -> bool:
    if not 0 <= gate_bits <= 32:
        raise ValueError("alien gate bits must be between 0 and 32")
    if gate_bits == 0:
        return True
    digest = alien_gate_digest(header, gate_string)
    threshold = 1 << (256 - gate_bits)
    return int.from_bytes(digest, "big") < threshold

# CYTHANX canonical 80-byte block header.
#
# Serialization is consensus-critical and deliberately independent of JSON:
#   version       uint32 LE   4 bytes
#   prev_hash     raw digest   32 bytes
#   merkle_root   raw digest   32 bytes
#   timestamp     uint32 LE   4 bytes
#   nBits         uint32 LE   4 bytes
#   nonce         uint32 LE   4 bytes
#                               --------
#                               80 bytes
#
# Hash fields are serialized as their 32-byte digest in natural byte order
# (the same order produced by hashlib.digest()). They are NOT reversed for
# display/serialization. Integer fields are unsigned little-endian.
# The payload is UTF-8 encoded and forms a single-leaf Merkle tree: its leaf
# hash is SHA256d(payload_bytes), which is therefore the Merkle root.

HEADER_SIZE = 80
HEADER_VERSION = 4

@dataclass
class Block:
    height: int
    timestamp: int
    previous_hash: str
    payload: str
    nonce: int = 0
    nbits: int = DEFAULT_NBITS
    version: int = HEADER_VERSION
    hash: str = ""

    def _hash32(self, value: str, field: str) -> bytes:
        try:
            raw = bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be hexadecimal") from exc
        if len(raw) != 32:
            raise ValueError(f"{field} must contain exactly 32 bytes / 64 hex characters")
        return raw

    def payload_bytes(self) -> bytes:
        return self.payload.encode("utf-8")

    def merkle_root(self) -> bytes:
        # Single-leaf Merkle tree. Multi-transaction Merkle construction can
        # replace this later without changing the 80-byte header layout.
        return sha256d(self.payload_bytes())

    def header_bytes(self) -> bytes:
        if not 0 <= self.version <= 0xFFFFFFFF:
            raise ValueError("version must fit uint32")
        if not 0 <= self.timestamp <= 0xFFFFFFFF:
            raise ValueError("timestamp must fit uint32")
        if not 0 <= self.nbits <= 0xFFFFFFFF:
            raise ValueError("nBits must fit uint32")
        if not 0 <= self.nonce <= 0xFFFFFFFF:
            raise ValueError("nonce must fit uint32")

        header = b"".join((
            self.version.to_bytes(4, "little"),
            self._hash32(self.previous_hash, "previous_hash"),
            self.merkle_root(),
            self.timestamp.to_bytes(4, "little"),
            self.nbits.to_bytes(4, "little"),
            self.nonce.to_bytes(4, "little"),
        ))
        if len(header) != HEADER_SIZE:
            raise AssertionError(f"CYTHANX header must be {HEADER_SIZE} bytes")
        return header

    def header(self) -> dict:
        # Human-readable representation; NEVER used for consensus hashing.
        return {
            "version": self.version,
            "height": self.height,
            "previous_hash": self.previous_hash,
            "merkle_root": self.merkle_root().hex(),
            "timestamp": self.timestamp,
            "nBits": f"{self.nbits:08x}",
            "nonce": self.nonce,
            "header_hex": self.header_bytes().hex(),
        }

    def layered_hash(self) -> bytes:
        header = self.header_bytes()
        # Layer order is deliberately explicit and consensus-critical.
        stage1 = blake3_256(b"CYTHANX|HEADER|" + header)
        stage2 = scrypt256(stage1, salt=header)
        stage3 = sha256d(b"CYTHANX|POW|SHA256D|" + stage2)
        return stage3

    def mine(
        self,
        target: int | None = None,
        max_nonce: int | None = None,
        alien_string: str = ALIEN_GATE_STRING,
        alien_gate_bits: int = ALIEN_GATE_BITS,
        dragon_enigma_key: bytes | None = None,
        dragon_gate_bits: int = 0,
    ) -> bool:
        # By default, proof-of-work difficulty is derived from the nBits field
        # committed inside this exact header. An explicit target remains
        # available for test vectors and controlled experiments.
        target_value = compact_to_target(self.nbits) if target is None else target
        if not 0 <= alien_gate_bits <= 32:
            raise ValueError("alien gate bits must be between 0 and 32")
        if not 0 < target_value < (1 << 256):
            raise ValueError("target must be in the range 1..2^256-1")
        if max_nonce is not None:
            if not 0 <= max_nonce <= 0xFFFFFFFF:
                raise ValueError("max_nonce must fit uint32")
            if self.nonce > max_nonce:
                return False
        while self.nonce <= 0xFFFFFFFF:
            header = self.header_bytes()
            digest = self.layered_hash()
            # Acceptance is gated immediately after the candidate PoW is
            # computed: no candidate is accepted unless BOTH conditions pass.
            pow_pass = int.from_bytes(digest, "big") <= target_value
            gate_pass = alien_gate_passes(header, alien_string, alien_gate_bits)

            dragon_pass = True
            if dragon_enigma_key is not None:
                dragon_digest = dragon_enigma_digest(
                    header, self.nonce, dragon_enigma_key
                )
                dragon_pass = dragon_lower_gate(
                    dragon_digest, dragon_gate_bits
                )

            if pow_pass and gate_pass and dragon_pass:
                self.hash = digest.hex()
                return True
            if max_nonce is not None and self.nonce >= max_nonce:
                return False
            if self.nonce == 0xFFFFFFFF:
                return False
            self.nonce += 1
        return False


# ---------------------------------------------------------------------------
# ORGANISM/TELEMETRY STATE
# ---------------------------------------------------------------------------

@dataclass
class OrganismState:
    energy: float = 1.0
    entropy: float = 0.0
    health: float = 1.0
    generation: int = 0

    def adapt(self, signal_value: bytes) -> None:
        x = int.from_bytes(blake3_256(signal_value)[:8], "big") / 2**64
        self.entropy = -math.log2(max(x, 2**-64)) / 64.0
        self.energy = max(0.0, min(2.0, self.energy * (1.0 + 0.02 + 0.08*x)))
        self.health = max(0.0, min(1.0, 0.97*self.health + 0.03*(1.0-abs(x-0.5))))
        self.generation += 1

def organism(seed: bytes, rounds: int = 3) -> dict:
    if rounds < 0:
        raise ValueError("organism rounds must be non-negative")
    state, previous = blake3_256(b"CYTHANX|GENESIS|" + seed), b""
    org = OrganismState()
    for i in range(rounds):
        payload = enigma_bytes(state + previous, sha256(seed + i.to_bytes(4, "big")))
        jo = joatt(payload, previous)
        vec = cythanize(bytes.fromhex(jo["state256"]), previous)
        transformed = transmogrify(bytes.fromhex(vec["commitment"]), 3 + i % 3)
        aux = blake3_256(b"CYTHANX|AUX|" + transformed)[:16]
        core_string, root = cyx_core_string(
            b"CYTHANX", b"CORE",
            state,
            bytes.fromhex(jo["state256"]),
            bytes.fromhex(vec["commitment"]),
            transformed,
            aux,
            i.to_bytes(8, "big"),
        )
        state = blake3_256(b"CYTHANX|STATE|" + core_string)
        org.adapt(root)
        previous = bytes.fromhex(jo["state256"])
    return {"state256": state.hex(), "organism": asdict(org)}


# ---------------------------------------------------------------------------
# BIP-39 / BIP-32 MNEMONIC DERIVATION BRIDGE
# ---------------------------------------------------------------------------
# This section implements the workflow represented by the supplied
# Transmogrification System diagram:
#   entropy -> SHA-256 checksum -> 11-bit words -> PBKDF2-HMAC-SHA512
#   -> BIP-32 master key -> BIP-44 Bitcoin address.
#
# The official 2048-word BIP-39 English list is deliberately NOT embedded.
# Pass its local path with --bip39-wordlist. This avoids silently substituting
# a non-standard word list.

SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
SECP256K1_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _nfkd(text: str) -> str:
    return unicodedata.normalize("NFKD", text)


def load_bip39_wordlist(path: Path) -> list[str]:
    words = [_nfkd(x.strip()) for x in path.read_text(encoding="utf-8").splitlines()
             if x.strip()]
    if len(words) != 2048 or len(set(words)) != 2048:
        raise ValueError("BIP-39 English wordlist must contain exactly 2048 unique words")
    return words


def bip39_entropy_to_mnemonic(entropy: bytes, words: list[str]) -> str:
    if len(entropy) not in (16, 20, 24, 28, 32):
        raise ValueError("BIP-39 entropy must be 128, 160, 192, 224, or 256 bits")
    checksum_bits = len(entropy) * 8 // 32
    digest = hashlib.sha256(entropy).digest()
    bits = int.from_bytes(entropy, "big") << checksum_bits
    bits |= int.from_bytes(digest, "big") >> (256 - checksum_bits)
    total = len(entropy) * 8 + checksum_bits
    count = total // 11
    return " ".join(words[(bits >> (total - 11 * (i + 1))) & 0x7ff] for i in range(count))


def bip39_mnemonic_to_entropy(mnemonic: str, words: list[str]) -> bytes:
    normalized = _nfkd(" ".join(mnemonic.strip().split()))
    parts = normalized.split(" ")
    if len(parts) not in (12, 15, 18, 21, 24):
        raise ValueError("BIP-39 mnemonic must contain 12, 15, 18, 21, or 24 words")
    index = {word: i for i, word in enumerate(words)}
    try:
        values = [index[w] for w in parts]
    except KeyError as exc:
        raise ValueError(f"word is not in the supplied BIP-39 wordlist: {exc.args[0]!r}") from exc
    total = len(parts) * 11
    stream = 0
    for value in values:
        stream = (stream << 11) | value
    checksum_bits = total // 33
    entropy_bits = total - checksum_bits
    entropy = (stream >> checksum_bits).to_bytes(entropy_bits // 8, "big")
    supplied = stream & ((1 << checksum_bits) - 1)
    expected = int.from_bytes(hashlib.sha256(entropy).digest(), "big") >> (256 - checksum_bits)
    if supplied != expected:
        raise ValueError("BIP-39 checksum mismatch")
    return entropy


def bip39_seed(mnemonic: str, passphrase: str = "") -> bytes:
    password = _nfkd(mnemonic).encode("utf-8")
    salt = ("mnemonic" + _nfkd(passphrase)).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha512", password, salt, 2048, 64)


def _inv(x: int, p: int = SECP256K1_P) -> int:
    return pow(x, p - 2, p)


def _point_add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    x1, y1 = a
    x2, y2 = b
    if x1 == x2 and (y1 + y2) % SECP256K1_P == 0:
        return None
    if a == b:
        m = (3 * x1 * x1) * _inv(2 * y1) % SECP256K1_P
    else:
        m = (y2 - y1) * _inv(x2 - x1) % SECP256K1_P
    x3 = (m * m - x1 - x2) % SECP256K1_P
    y3 = (m * (x1 - x3) - y1) % SECP256K1_P
    return x3, y3


def _point_mul(k: int, point=(SECP256K1_GX, SECP256K1_GY)):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


def secp256k1_pubkey(private_key: bytes, compressed: bool = True) -> bytes:
    k = int.from_bytes(private_key, "big")
    if not 1 <= k < SECP256K1_N:
        raise ValueError("invalid secp256k1 private key")
    x, y = _point_mul(k)
    if compressed:
        return bytes([2 | (y & 1)]) + x.to_bytes(32, "big")
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def bip32_master(seed: bytes) -> tuple[bytes, bytes]:
    i = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    key, chain = i[:32], i[32:]
    if not 1 <= int.from_bytes(key, "big") < SECP256K1_N:
        raise ValueError("invalid BIP-32 master key")
    return key, chain


def bip32_ckd_priv(key: bytes, chain: bytes, index: int) -> tuple[bytes, bytes]:
    if not 0 <= index <= 0xffffffff:
        raise ValueError("BIP-32 child index out of range")
    if index >= 0x80000000:
        data = b"\x00" + key + index.to_bytes(4, "big")
    else:
        data = secp256k1_pubkey(key) + index.to_bytes(4, "big")
    i = hmac.new(chain, data, hashlib.sha512).digest()
    child = (int.from_bytes(i[:32], "big") + int.from_bytes(key, "big")) % SECP256K1_N
    if int.from_bytes(i[:32], "big") >= SECP256K1_N or child == 0:
        raise ValueError("BIP-32 invalid child; retry with next valid derivation index")
    return child.to_bytes(32, "big"), i[32:]


def bip32_derive_path(key: bytes, chain: bytes, path: str) -> tuple[bytes, bytes]:
    if path in ("m", ""):
        return key, chain
    if not path.startswith("m/"):
        raise ValueError("BIP-32 path must start with m/")
    for component in path[2:].split("/"):
        hardened = component.endswith("'") or component.endswith("h")
        number = component[:-1] if hardened else component
        if not number.isdigit():
            raise ValueError(f"invalid BIP-32 path component: {component}")
        index = int(number)
        if index >= 0x80000000:
            raise ValueError("non-hardened index exceeds BIP-32 range")
        if hardened:
            index |= 0x80000000
        key, chain = bip32_ckd_priv(key, chain, index)
    return key, chain


def base58_encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = B58[rem] + out
    return B58[0] * (len(raw) - len(raw.lstrip(b"\0"))) + (out or "")


def base58check(payload: bytes) -> str:
    return base58_encode(payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4])


def bip32_xprv(key: bytes, chain: bytes, depth: int, parent_fp: bytes, child_index: int) -> str:
    payload = (b"\x04\x88\xad\xe4" + bytes([depth]) + parent_fp +
               child_index.to_bytes(4, "big") + chain + b"\x00" + key)
    return base58check(payload)


def bip32_xpub(key: bytes, chain: bytes, depth: int, parent_fp: bytes, child_index: int) -> str:
    pub = secp256k1_pubkey(key)
    payload = (b"\x04\x88\xb2\x1e" + bytes([depth]) + parent_fp +
               child_index.to_bytes(4, "big") + chain + pub)
    return base58check(payload)


def hash160(data: bytes) -> bytes:
    try:
        ripe = hashlib.new("ripemd160")
    except ValueError as exc:
        raise RuntimeError("Python/OpenSSL RIPEMD160 is required for Bitcoin P2PKH") from exc
    ripe.update(hashlib.sha256(data).digest())
    return ripe.digest()


def bitcoin_p2pkh(pubkey: bytes) -> str:
    # Mainnet P2PKH: version 0x00 + HASH160(compressed public key) + checksum.
    return base58check(b"\x00" + hash160(pubkey))


def bip39_derive_report(mnemonic: str, passphrase: str, words: list[str],
                        path: str = "m/44'/0'/0'/0/0") -> dict:
    entropy = bip39_mnemonic_to_entropy(mnemonic, words)
    seed = bip39_seed(mnemonic, passphrase)
    master_key, master_chain = bip32_master(seed)
    child_key, child_chain = bip32_derive_path(master_key, master_chain, path)

    # BIP-32 metadata for the selected path.
    components = path[2:].split("/") if path != "m" else []
    parent_key, parent_chain = bip32_derive_path(master_key, master_chain,
                                                  "m" if len(components) <= 1 else
                                                  "m/" + "/".join(components[:-1]))
    parent_pub = secp256k1_pubkey(parent_key)
    parent_fp = hash160(parent_pub)[:4]
    child_index = 0 if not components else (
        int(components[-1].rstrip("'h")) |
        (0x80000000 if components[-1].endswith(("'", "h")) else 0)
    )
    pub = secp256k1_pubkey(child_key)
    return {
        "entropy_hex": entropy.hex(),
        "entropy_bits": len(entropy) * 8,
        "checksum_bits": len(entropy) * 8 // 32,
        "mnemonic_word_count": len(mnemonic.split()),
        "pbkdf2": "PBKDF2-HMAC-SHA512",
        "iterations": 2048,
        "seed_512_hex": seed.hex(),
        "master_private_key_hex": master_key.hex(),
        "master_chain_code_hex": master_chain.hex(),
        "path": path,
        "child_private_key_hex": child_key.hex(),
        "child_chain_code_hex": child_chain.hex(),
        "child_public_key_hex": pub.hex(),
        "bitcoin_p2pkh": bitcoin_p2pkh(pub),
        "master_xprv": bip32_xprv(master_key, master_chain, 0, b"\0"*4, 0),
        "master_xpub": bip32_xpub(master_key, master_chain, 0, b"\0"*4, 0),
        "derived_xprv": bip32_xprv(child_key, child_chain, len(components),
                                    parent_fp, child_index),
        "derived_xpub": bip32_xpub(child_key, child_chain, len(components),
                                    parent_fp, child_index),
    }


def bip39_generate_report(words: list[str], passphrase: str = "",
                           path: str = "m/44'/0'/0'/0/0") -> dict:
    entropy = secrets.token_bytes(32)  # 24 words / 256 bits
    mnemonic = bip39_entropy_to_mnemonic(entropy, words)
    report = bip39_derive_report(mnemonic, passphrase, words, path)
    # Never expose the generated mnemonic through an implicit side effect in
    # the CYTHANX miner. It is printed only when this explicit mode is used.
    report["mnemonic"] = mnemonic
    return report


def mnemonic_fusion(mnemonic: str, passphrase: str, words: list[str]) -> dict:
    """Bridge the BIP-39 seed into the existing CYTHANX fusion pipeline."""
    seed = bip39_seed(mnemonic, passphrase)
    j = joatt(seed)
    c = cythanize(bytes.fromhex(j["state256"]))
    transformed = transmogrify(bytes.fromhex(c["commitment"]))
    root = blake3_256(b"CYTHANX|BIP39-FUSION|" + seed + transformed)
    return {
        "bip39_seed_sha256": sha256(seed).hex(),
        "joatt_state256": j["state256"],
        "cythan_commitment": c["commitment"],
        "transmogrified": transformed.hex(),
        "fusion_root": root.hex(),
    }


# ---------------------------------------------------------------------------
# CYTHANX MOG STRATEGIC CONTROL
# ---------------------------------------------------------------------------

MOG_VERSION = "1.0"

def mog_status() -> dict:
    """Return a non-secret structural status snapshot."""
    return {
        "name": "CYTHANX MOG",
        "version": MOG_VERSION,
        "architecture": [
            "validate",
            "fuse",
            "organism",
            "mine-block",
        ],
        "blake3_backend": "blake3" if _blake3 else "blake2b-fallback",
        "header_size": HEADER_SIZE,
        "default_nbits": f"{DEFAULT_NBITS:08x}",
        "native_pow": True,
        "alien_string_gate": True,
        "alien_gate_bits": ALIEN_GATE_BITS,
        "alien_gate_string": ALIEN_GATE_STRING,
        "dragon_enigma": True,
        "dragon_enigma_primitives": ["SHA-256", "SHAKE256", "HKDF-HMAC-SHA256"],
        "dragon_lower_gate_bits": 8,
        "persistent_share_ledger": str(LEDGER_FILE),
        "genesis_verification": True,
        "joatt_conformance": True,
        "wallet_or_password_embedded": False,
    }

def run_mog(args: argparse.Namespace) -> int:
    """Central strategic controller; executes one selected operating mode."""
    print(json.dumps({
        "event": "mog_start",
        "version": MOG_VERSION,
        "mode": args.procedure or "validate",
    }, indent=2))

    if args.procedure == "validate":
        print(json.dumps(self_test(), indent=2))
        return 0

    if args.procedure == "fuse":
        paths = args.fuse or [Path(__file__)]
        result = fuse_files(paths, args.seed)
        print(json.dumps(result, indent=2))
        return 0

    if args.procedure == "organism":
        seed = sha256((args.seed or "CYTHANX-MOG").encode())
        print(json.dumps(organism(seed, rounds=max(1, args.organism_rounds)), indent=2))
        return 0

    if args.procedure == "mine-block":
        seed = sha256((args.seed or "CYTHANX-MOG").encode())
        payload = cythanize(seed)["commitment"]
        block = Block(0, int(time.time()), "00" * 32, payload)
        dragon_key = bytes.fromhex(args.dragon_enigma_key) if args.dragon_enigma_key else None
        if block.mine(
            max_nonce=args.max_nonce,
            alien_string=args.alien_string,
            alien_gate_bits=args.alien_gate_bits,
            dragon_enigma_key=dragon_key,
            dragon_gate_bits=args.dragon_gate_bits,
        ):
            ledger = ShareLedger(args.ledger_file)
            record = ledger.append_share(
                block, source="local-mine",
                alien_string=args.alien_string,
                alien_gate_bits=args.alien_gate_bits,
                dragon_enigma_key=dragon_key,
                dragon_gate_bits=args.dragon_gate_bits,
            )
            result = asdict(block)
            result["ledger_record"] = record
            result["ledger"] = ledger.summary()
            print(json.dumps(result, indent=2))
            return 0
        print(json.dumps({
            "event": "bounded_mining_exhausted",
            "max_nonce": args.max_nonce,
        }, indent=2))
        return 1

    if args.procedure == "genesis-verify":
        ok, genesis, reason = verify_genesis()
        print(json.dumps({"ok": ok, "reason": reason, "genesis": genesis}, indent=2))
        return 0 if ok else 1

    if args.procedure == "ledger-verify":
        ledger = ShareLedger(args.ledger_file)
        result = ledger.summary()
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    if args.procedure == "dragonfly":
        return run_dragonfly(args)

    raise SystemExit("Select --procedure {validate,fuse,organism,mine-block,genesis-verify,ledger-verify,dragonfly}")


# ---------------------------------------------------------------------------
# CLI / SELF TEST
# ---------------------------------------------------------------------------


def run_dragonfly(args: argparse.Namespace) -> int:
    """Run the complete native Dragonfly mining organism in one operation.

    Order is deliberately fixed:
        genesis verification -> ledger verification -> organism work state
        -> JOATT work commitment -> native CYTHANX PoW/gates -> ledger append
        -> post-acceptance ledger verification.

    This is a CYTHANX miner. It does not manufacture Bitcoin shares or imply
    BTC payout; any Bitcoin proof-of-work remains independently verifiable.
    """
    start = time.time()
    genesis_ok, genesis, genesis_reason = verify_genesis()
    if not genesis_ok:
        print(json.dumps({"ok": False, "stage": "genesis", "reason": genesis_reason}, indent=2))
        return 2

    ledger = ShareLedger(args.ledger_file)
    ledger_before = ledger.summary()
    if not ledger_before["ok"]:
        print(json.dumps({"ok": False, "stage": "ledger-before", **ledger_before}, indent=2))
        return 2

    dragon_key = bytes.fromhex(args.dragon_enigma_key) if args.dragon_enigma_key else None
    if dragon_key is not None and len(dragon_key) == 0:
        raise SystemExit("--dragon-enigma-key must not be empty")

    seed = sha256((args.seed or "CYTHANX-DRAGONFLY").encode("utf-8"))
    organism_report = organism(seed, rounds=max(1, args.dragonfly_rounds))
    organism_state = bytes.fromhex(organism_report["state256"])

    # The organism is the work source: genesis + organism state are committed
    # into the payload before the candidate header is constructed.
    work_material = DRAGONFLY_DOMAIN + bytes.fromhex(genesis["layered_hash"]) + organism_state
    work_root = blake3_256(work_material)
    payload = cythanize(work_root)["commitment"]

    block = Block(
        height=1,
        timestamp=int(time.time()),
        previous_hash=genesis["layered_hash"],
        payload=payload,
    )

    print(json.dumps({
        "event": "dragonfly_start",
        "organism": "CYTHANX",
        "genesis_verified": True,
        "genesis_layered_hash": genesis["layered_hash"],
        "organism_state256": organism_report["state256"],
        "organism_rounds": max(1, args.dragonfly_rounds),
        "work_root": work_root.hex(),
        "ledger": str(args.ledger_file),
        "alien_gate_bits": args.alien_gate_bits,
        "dragon_gate_bits": args.dragon_gate_bits,
        "max_nonce": args.max_nonce,
    }, indent=2))

    try:
        found = block.mine(
            max_nonce=args.max_nonce,
            alien_string=args.alien_string,
            alien_gate_bits=args.alien_gate_bits,
            dragon_enigma_key=dragon_key,
            dragon_gate_bits=args.dragon_gate_bits,
        )
    except KeyboardInterrupt:
        print(json.dumps({
            "ok": False,
            "stage": "mining",
            "event": "dragonfly_interrupted",
            "nonce": block.nonce,
            "elapsed_seconds": round(time.time() - start, 3),
        }, indent=2))
        return 130

    if not found:
        print(json.dumps({
            "ok": False,
            "stage": "mining",
            "event": "dragonfly_window_exhausted",
            "max_nonce": args.max_nonce,
            "elapsed_seconds": round(time.time() - start, 3),
            "ledger": ledger_before,
        }, indent=2))
        return 1

    record = ledger.append_share(
        block,
        source="dragonfly-organism",
        alien_string=args.alien_string,
        alien_gate_bits=args.alien_gate_bits,
        dragon_enigma_key=dragon_key,
        dragon_gate_bits=args.dragon_gate_bits,
    )
    ledger_after = ledger.summary()
    if not ledger_after["ok"]:
        print(json.dumps({"ok": False, "stage": "ledger-after", **ledger_after}, indent=2))
        return 2

    print(json.dumps({
        "ok": True,
        "event": "dragonfly_share_accepted",
        "organism": "CYTHANX",
        "work_root": work_root.hex(),
        "block": asdict(block),
        "joatt": {
            "state256": record["joatt_state256"],
            "identifier64": record["joatt_identifier64"],
            "previous_state256": record["joatt_previous_state256"],
            "input_sha256d": record["joatt_input_sha256d"],
        },
        "ledger": ledger_after,
        "elapsed_seconds": round(time.time() - start, 3),
    }, indent=2))
    return 0


def run_dragonfly_shell(args: argparse.Namespace, auto_mine: bool = False, pool: str = "", worker: str = "dragonfly", user: str = "") -> int:
    """Interactive CYTHANX Dragonfly organism shell.

    All operational choices are made inside this prompt.  The Dragonfly
    organism becomes the work source for block 1 and every subsequent block
    mined during the session.  No external miner or shell command is used.
    """
    print("=" * 72)
    print("CYTHANX DRAGONFLY — ORGANISM-OF-WORK SHELL")
    print("All operations remain inside this Python prompt.")
    print("=" * 72)

    genesis_ok, genesis, reason = verify_genesis()
    if not genesis_ok:
        print(f"[GENESIS] FAILED: {reason}")
        return 2

    ledger = ShareLedger(args.ledger_file)
    summary = ledger.summary()
    if not summary["ok"]:
        print(f"[LEDGER] FAILED: {summary}")
        return 2

    dragon_key = bytes.fromhex(args.dragon_enigma_key) if args.dragon_enigma_key else None
    seed_text = args.seed or "CYTHANX-DRAGONFLY"
    organism_rounds = max(1, args.dragonfly_rounds)
    max_nonce = args.max_nonce
    alien_string = args.alien_string
    alien_bits = args.alien_gate_bits
    dragon_bits = args.dragon_gate_bits

    height = 1
    previous_hash = genesis["layered_hash"]
    organism_seed = sha256(seed_text.encode("utf-8"))
    last_block = None
    last_organism = None

    def status() -> None:
        current = ledger.summary()
        print(json.dumps({
            "shell": "CYTHANX-DRAGONFLY",
            "organism": "CYTHANX",
            "genesis_verified": genesis_ok,
            "next_height": height,
            "previous_hash": previous_hash,
            "organism_rounds": organism_rounds,
            "alien_gate_bits": alien_bits,
            "dragon_gate_bits": dragon_bits,
            "max_nonce": max_nonce,
            "ledger": current,
            "last_block": asdict(last_block) if last_block else None,
            "last_organism": last_organism,
        }, indent=2))

    def mine_one() -> bool:
        nonlocal height, previous_hash, organism_seed, last_block, last_organism

        # The organism evolves from the previous accepted block and remains
        # the shell's work source for the next block.
        work_seed = sha256(
            b"CYTHANX|DRAGONFLY|NEXT|" +
            bytes.fromhex(previous_hash) +
            height.to_bytes(8, "big") +
            organism_seed
        )
        report = organism(work_seed, rounds=organism_rounds)
        state = bytes.fromhex(report["state256"])
        work_material = (
            DRAGONFLY_DOMAIN +
            bytes.fromhex(previous_hash) +
            height.to_bytes(8, "big") +
            state
        )
        work_root = blake3_256(work_material)
        payload = cythanize(work_root)["commitment"]

        block = Block(
            height=height,
            timestamp=int(time.time()),
            previous_hash=previous_hash,
            payload=payload,
        )
        print(json.dumps({
            "event": "dragonfly_mining",
            "organism": "CYTHANX",
            "height": height,
            "previous_hash": previous_hash,
            "organism_state256": report["state256"],
            "work_root": work_root.hex(),
            "max_nonce": max_nonce,
            "alien_gate_bits": alien_bits,
            "dragon_gate_bits": dragon_bits,
        }, indent=2))

        try:
            found = block.mine(
                max_nonce=max_nonce,
                alien_string=alien_string,
                alien_gate_bits=alien_bits,
                dragon_enigma_key=dragon_key,
                dragon_gate_bits=dragon_bits,
            )
        except KeyboardInterrupt:
            print("[DRAGONFLY] mining interrupted; no share was appended.")
            return False

        if not found:
            print(json.dumps({
                "ok": False,
                "event": "dragonfly_window_exhausted",
                "height": height,
                "max_nonce": max_nonce,
                "ledger": ledger.summary(),
            }, indent=2))
            return False

        try:
            record = ledger.append_share(
                block,
                source="dragonfly-organism-shell",
                alien_string=alien_string,
                alien_gate_bits=alien_bits,
                dragon_enigma_key=dragon_key,
                dragon_gate_bits=dragon_bits,
            )
        except (RuntimeError, ValueError) as exc:
            print(f"[DRAGONFLY] acceptance/ledger check failed: {exc}")
            return False

        verified, state_info, verify_reason = ledger.verify()
        if not verified:
            print(f"[LEDGER] POST-ACCEPTANCE VERIFICATION FAILED: {verify_reason}")
            return False

        print(json.dumps({
            "ok": True,
            "event": "dragonfly_block_accepted",
            "height": block.height,
            "block_hash": block.layered_hash().hex(),
            "nonce": block.nonce,
            "organism_state256": report["state256"],
            "joatt_state256": record["joatt_state256"],
            "joatt_identifier64": record["joatt_identifier64"],
            "ledger": state_info,
        }, indent=2))

        last_block = block
        last_organism = report
        previous_hash = block.layered_hash().hex()
        organism_seed = state
        height += 1
        return True

    if auto_mine:
        print(json.dumps({
            "mode": "auto-mine",
            "organism": "CYTHANX-DRAGONFLY",
            "pool": pool or "local/native",
            "user": user or "local-user",
            "worker": worker,
            "note": "Pool endpoint is session metadata only. External pool submission requires a compatible pool protocol; this native miner does not fabricate pool shares."
        }, indent=2))
        print("[AUTO-MINE] Mining successive Dragonfly blocks. Press Ctrl-C to stop.")
        try:
            while True:
                if not mine_one():
                    print("[AUTO-MINE] Current bounded nonce window exhausted; stopping safely.")
                    return 1
        except KeyboardInterrupt:
            print("\n[AUTO-MINE] stopped by user.")
            return 0

    print("\nCommands are entered here only:")
    print("  start   initialize/mine the next Dragonfly block")
    print("  mine    mine the next block")
    print("  status  show organism, chain, gate, and ledger state")
    print("  verify  verify genesis and ledger")
    print("  quit    leave the shell")

    while True:
        try:
            command = input("\nCYTHANX-DRAGONFLY> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[DRAGONFLY] shell stopped.")
            return 0

        if command in {"quit", "exit", "q"}:
            print("[DRAGONFLY] shell stopped.")
            return 0
        if command in {"status", "s"}:
            status()
            continue
        if command in {"verify", "v"}:
            ok, info, why = ledger.verify()
            print(json.dumps({"genesis": genesis_ok, "ledger_ok": ok,
                              "reason": why, "ledger": info}, indent=2))
            continue
        if command in {"start", "mine", "m"}:
            mine_one()
            continue
        if command == "":
            continue
        print("[PROMPT] Use: start | mine | status | verify | quit")



# ---------------------------------------------------------------------------
# RANDOMX / STRATUM-V1 POOL COMPATIBILITY LAYER
# ---------------------------------------------------------------------------
# This layer intentionally does not invent or transform shares.  It speaks the
# common line-delimited Stratum V1 JSON-RPC shape used by many RandomX pools,
# tracks changing jobs, and delegates the actual RandomX hash to a native
# backend when one is available.  A pool URL is never treated as CYTHANX PoW.

class RandomXBackendUnavailable(RuntimeError):
    pass


def _randomx_hash_native(blob: bytes, seed_hash: bytes | None = None) -> bytes:
    """Use an installed Python RandomX binding if it exposes a known API.

    Different bindings expose different names, so this adapter accepts several
    common call shapes.  It refuses to fake a RandomX result when no backend is
    available.
    """
    candidates = []
    try:
        import randomx  # type: ignore
        candidates.append(randomx)
    except ImportError:
        randomx = None

    for mod in candidates:
        for name in ("get_rx_hash", "randomx_hash", "hash", "calculate_hash"):
            fn = getattr(mod, name, None)
            if callable(fn):
                for args in ((blob,), (blob, seed_hash) if seed_hash else (blob,)):
                    try:
                        out = fn(*args)
                        if isinstance(out, str):
                            out = bytes.fromhex(out)
                        if isinstance(out, (bytes, bytearray)) and len(out) == 32:
                            return bytes(out)
                    except TypeError:
                        continue
                    except Exception:
                        continue

        # Common class-style bindings: RandomXVM(key).calculate(blob).
        for cls_name in ("RandomXVM", "RandomX", "RandomXHasher"):
            cls = getattr(mod, cls_name, None)
            if cls is None:
                continue
            try:
                vm = cls(seed_hash or b"" )
                for name in ("calculate", "hash", "calculate_hash"):
                    fn = getattr(vm, name, None)
                    if callable(fn):
                        out = fn(blob)
                        if isinstance(out, str):
                            out = bytes.fromhex(out)
                        if isinstance(out, (bytes, bytearray)) and len(out) == 32:
                            return bytes(out)
            except Exception:
                continue

    raise RandomXBackendUnavailable(
        "No usable RandomX backend is installed. Pool compatibility is enabled, "
        "but valid RandomX shares cannot be produced until a native RandomX "
        "Python binding is installed. No fake share will be submitted."
    )


def _parse_pool_endpoint(endpoint: str) -> tuple[str, int, bool]:
    endpoint = endpoint.strip()
    if not endpoint:
        raise ValueError("pool endpoint is empty")
    secure = False
    if endpoint.startswith("stratum+ssl://"):
        secure = True
        endpoint = endpoint[len("stratum+ssl://"):]
    elif endpoint.startswith("stratum+tcp://"):
        endpoint = endpoint[len("stratum+tcp://"):]
    elif endpoint.startswith("tcp://"):
        endpoint = endpoint[len("tcp://"):]
    elif endpoint.startswith("ssl://"):
        secure = True
        endpoint = endpoint[len("ssl://"):]
    if ":" not in endpoint:
        raise ValueError("pool must be HOST:PORT (for example pool.example:3333)")
    host, port_text = endpoint.rsplit(":", 1)
    return host.strip(), int(port_text), secure


class RandomXStratumV1Client:
    """Small Stratum V1 client for dynamic RandomX pool jobs.

    Supported protocol flow:
      mining.subscribe -> mining.authorize -> mining.notify/set_target
      -> local RandomX hashing -> mining.submit

    The client treats every notify as a new job and abandons the previous job.
    It does not submit anything unless the RandomX backend returns a real
    32-byte digest and the locally calculated share target is satisfied.
    """

    def __init__(self, endpoint: str, user: str, worker: str, password: str = "x",
                 timeout: float = 15.0) -> None:
        import socket
        self.endpoint = endpoint
        self.user = user
        self.worker = worker
        self.password = password or "x"
        self.timeout = timeout
        self.sock = None
        self.reader = None
        self.next_id = 1
        self.job = None
        self.target = None
        self.accepted = 0
        self.rejected = 0
        self.stale = 0
        self.socket_module = socket

    def _send(self, method: str, params: list[Any], request_id: int | None = None) -> int:
        if request_id is None:
            request_id = self.next_id
            self.next_id += 1
        msg = {"id": request_id, "jsonrpc": "2.0", "method": method, "params": params}
        self.sock.sendall((json.dumps(msg, separators=(",", ":")) + "\n").encode())
        return request_id

    def connect(self) -> None:
        host, port, secure = _parse_pool_endpoint(self.endpoint)
        raw = self.socket_module.create_connection((host, port), timeout=self.timeout)
        if secure:
            import ssl
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)
        raw.settimeout(self.timeout)
        self.sock = raw
        self.reader = raw.makefile("rb")
        self._send("mining.subscribe", ["cythanx-randomx/1.0", None, ""])

    def close(self) -> None:
        try:
            if self.reader:
                self.reader.close()
        except Exception:
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.reader = None
        self.sock = None

    def _recv(self) -> dict[str, Any] | None:
        line = self.reader.readline()
        if not line:
            return None
        return json.loads(line.decode("utf-8", "replace"))

    def _target_int(self, target: Any) -> int | None:
        if target is None:
            return None
        if isinstance(target, int):
            return target
        if isinstance(target, str):
            try:
                return int(target, 16)
            except ValueError:
                try:
                    return int(target)
                except ValueError:
                    return None
        return None

    def _job_blob(self, job: dict[str, Any]) -> bytes:
        blob = job.get("blob")
        if isinstance(blob, str):
            return bytes.fromhex(blob)
        # Some RandomX pools provide seed/height rather than a ready-made blob.
        # Building a pool-specific blob without knowing its protocol is unsafe,
        # so require the standard blob form instead of guessing.
        raise ValueError("RandomX job has no hexadecimal 'blob'; pool-specific blob construction is not implemented")

    def _handle_message(self, msg: dict[str, Any]) -> bool:
        method = msg.get("method")
        params = msg.get("params") or []

        if method == "mining.notify":
            # Common params: job_id, blob, target, ... .  Pools differ, so also
            # accept a dictionary-shaped final parameter when available.
            job = {"raw": params}
            if params and isinstance(params[0], dict):
                job.update(params[0])
            else:
                if len(params) >= 1: job["job_id"] = params[0]
                if len(params) >= 2: job["blob"] = params[1]
                if len(params) >= 3: job["target"] = params[2]
                if len(params) >= 4: job["seed_hash"] = params[3]
            self.job = job
            if job.get("target") is not None:
                self.target = self._target_int(job.get("target"))
            print(json.dumps({"event":"randomx_new_job", "job_id":job.get("job_id"),
                              "target":job.get("target"), "non_deterministic":True}, indent=2))
            return True

        if method == "mining.set_target" and params:
            self.target = self._target_int(params[0])
            print(json.dumps({"event":"randomx_set_target", "target":params[0]}, indent=2))
            return True

        if method == "client.reconnect":
            raise ConnectionError("pool requested reconnect")

        if "result" in msg and msg.get("id") == 1:
            login = self.user if (not self.worker or self.user.endswith("." + self.worker)) else self.user + "." + self.worker
            self._send("mining.authorize", [login, self.password])
            return True

        if "result" in msg and msg.get("id") is not None:
            return True

        return True

    def mine_job_once(self, nonce_count: int = 1024) -> dict[str, Any]:
        """Try a bounded nonce window for the current pool job.

        RandomX pools commonly place a 4-byte nonce at offset 39 in the blob.
        The blob is copied for each nonce so a valid result is tied to the exact
        job received from the pool.  A bounded window lets the receive loop
        process a replacement job rather than mining an obsolete job forever.
        """
        if not self.job:
            return {"ok": False, "reason": "no active RandomX job"}
        job = self.job
        job_id = job.get("job_id")
        base_blob = self._job_blob(job)
        if len(base_blob) < 43:
            return {"ok": False, "reason": "RandomX blob is too short for the standard 4-byte nonce field"}
        seed_hex = job.get("seed_hash")
        seed = bytes.fromhex(seed_hex) if isinstance(seed_hex, str) else None
        target = self._target_int(job.get("target"))
        if target is None:
            target = self.target
        if target is None:
            return {"ok": False, "reason": "job has no usable share target"}

        # Preserve an explicit pool-provided starting nonce when present.
        try:
            start_nonce = int(str(job.get("nonce", "0")), 16) if isinstance(job.get("nonce"), str) else int(job.get("nonce", 0))
        except ValueError:
            start_nonce = 0

        for offset in range(max(1, nonce_count)):
            nonce = (start_nonce + offset) & 0xffffffff
            blob = bytearray(base_blob)
            blob[39:43] = nonce.to_bytes(4, "little")
            digest = _randomx_hash_native(bytes(blob), seed)
            if int.from_bytes(digest, "little") > target:
                continue

            nonce_hex = nonce.to_bytes(4, "little").hex()
            request_id = self._send("mining.submit", [self.user, job_id, nonce_hex, digest.hex()])
            return {"ok": True, "submitted": True, "request_id": request_id,
                    "job_id": job_id, "hash": digest.hex(), "nonce": nonce_hex}

        return {"ok": False, "reason": "bounded nonce window exhausted", "job_id": job_id,
                "tested": max(1, nonce_count)}

    def run(self) -> int:
        print(json.dumps({"mode":"randomx-stratum-v1", "pool":self.endpoint,
                          "user":self.user, "worker":self.worker,
                          "note":"Dynamic jobs replace prior jobs; no fabricated shares."}, indent=2))
        self.connect()
        try:
            while True:
                msg = self._recv()
                if msg is None:
                    raise ConnectionError("pool closed connection")
                self._handle_message(msg)
                # Hash only after a notify has established a job.  The pool may
                # send a replacement at any time; the next received message is
                # processed before another submission.
                if self.job:
                    try:
                        result = self.mine_job_once()
                    except RandomXBackendUnavailable as exc:
                        print(f"[RANDOMX] {exc}")
                        return 2
                    except ValueError as exc:
                        print(f"[RANDOMX] {exc}")
                        continue
                    print(json.dumps(result, indent=2))
        finally:
            self.close()


def run_randomx_pool_prompt(args: argparse.Namespace) -> int:
    print("\n[RANDOMX POOL] Stratum V1 dynamic-job compatibility")
    pool = input("CYTHANX pool> ").strip()
    user = input("CYTHANX user> ").strip()
    worker = input("CYTHANX worker [dragonfly]> ").strip() or "dragonfly"
    password = input("CYTHANX password [x]> ").strip() or "x"
    if not pool or not user:
        print("[RANDOMX POOL] pool and user are required.")
        return 1
    try:
        return RandomXStratumV1Client(pool, user, worker, password).run()
    except (OSError, ValueError, ConnectionError) as exc:
        print(f"[RANDOMX POOL] {exc}")
        return 1


def automated_setup_prompt(args: argparse.Namespace) -> int:
    """Single first-run setup prompt for the consolidated miner."""
    print("=" * 72)
    print("CYTHANX DRAGONFLY — CONSOLIDATED AUTOMATED MINER")
    print("The Python script replaces the separate shell launcher.")
    print("=" * 72)

    def ask(prompt, default=""):
        value = input(f"{prompt}" + (f" [{default}]" if default != "" else "") + ": ").strip()
        return value if value else default

    mode = ask(
        "Mode: auto-mine / prompt / validate",
        "auto-mine",
    ).lower()

    if mode in {"validate", "v"}:
        print(json.dumps(self_test(), indent=2))
        return 0
    if mode in {"prompt", "p", "menu"}:
        return prompt_controller(args)

    if mode not in {"auto-mine", "auto", "mine", "m"}:
        print("[PROMPT] Unknown mode; using auto-mine.")
        mode = "auto-mine"

    args.seed = ask("Seed", args.seed or "CYTHANX-DRAGONFLY")
    args.organism_rounds = max(
        1, int(ask("Organism rounds", str(args.dragonfly_rounds or 3)))
    )
    args.dragonfly_rounds = args.organism_rounds
    args.max_nonce = int(
        ask("Maximum nonce per block (0-4294967295)", str(args.max_nonce or 1000))
    )
    args.alien_gate_bits = int(
        ask("Alien gate bits", str(args.alien_gate_bits))
    )
    args.dragon_gate_bits = int(
        ask("Dragon gate bits (0 disables)", str(args.dragon_gate_bits))
    )

    pool = ask(
        "Pool endpoint/name (blank = local/native CYTHANX)",
        "",
    )
    user = ask("Pool user", "anonymous")
    worker = ask("Worker", "dragonfly")

    print("\n[SETUP] Configuration accepted.")
    print(f"[SETUP] Seed: {args.seed}")
    print(f"[SETUP] Rounds: {args.dragonfly_rounds}")
    print(f"[SETUP] Max nonce: {args.max_nonce}")
    print(f"[SETUP] Alien gate bits: {args.alien_gate_bits}")
    print(f"[SETUP] Dragon gate bits: {args.dragon_gate_bits}")
    print(f"[SETUP] Pool: {pool or 'local/native'}")
    print(f"[SETUP] Worker: {worker}")

    # Preserve the source miner's rule: a pool value here is session metadata
    # for native Dragonfly auto-mining.  Actual RandomX/Stratum operation uses
    # its dedicated pool mode and requires a real RandomX backend.
    if pool:
        print(
            "[POOL] For actual Stratum V1 RandomX work, choose "
            "'pool-mine' from the main prompt."
        )

    return run_dragonfly_shell(
        args,
        auto_mine=True,
        pool=pool,
        worker=worker,
        user=user,
    )

def prompt_controller(args: argparse.Namespace) -> int:
    """Single in-script command prompt for all CYTHANX operations."""
    print("=" * 72)
    print("CYTHANX NATIVE MINER — SINGLE OPERATION PROMPT")
    print("No external miner and no external shell command is required.")
    print("=" * 72)
    print("1  validate")
    print("2  genesis-verify")
    print("3  ledger-verify")
    print("4  organism")
    print("5  dragonfly")
    print("6  auto-mine")
    print("7  pool-mine (RandomX/Stratum V1)")
    print("8  mine-block")
    print("9  status")
    print("q  quit")

    while True:
        try:
            choice = input("\nCYTHANX> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[CYTHANX] stopped.")
            return 0

        if choice in {"q", "quit", "exit"}:
            print("[CYTHANX] stopped.")
            return 0
        if choice in {"5", "dragonfly"}:
            return_code = run_dragonfly_shell(args)
            if return_code not in {0, 1}:
                return return_code
            continue
        if choice in {"1", "validate"}:
            print(json.dumps(self_test(), indent=2))
            continue
        if choice in {"2", "genesis", "genesis-verify"}:
            ok, genesis, reason = verify_genesis()
            print(json.dumps({"ok": ok, "reason": reason, "genesis": genesis}, indent=2))
            continue
        if choice in {"3", "ledger", "ledger-verify"}:
            ledger = ShareLedger(args.ledger_file)
            print(json.dumps(ledger.summary(), indent=2))
            continue
        if choice in {"4", "organism"}:
            seed = sha256((args.seed or "CYTHANX-PROMPT").encode("utf-8"))
            print(json.dumps(organism(seed, rounds=max(1, args.organism_rounds)), indent=2))
            continue
        if choice in {"6", "auto", "auto-mine"}:
            print("\n[POOL] Enter the pool endpoint/name for this session.")
            print("[POOL] Leave blank for local/native CYTHANX mining.")
            pool = input("CYTHANX pool> ").strip()
            user = input("CYTHANX user [anonymous]> ").strip() or "anonymous"
            worker = input("CYTHANX worker [dragonfly]> ").strip() or "dragonfly"
            return run_dragonfly_shell(args, auto_mine=True, pool=pool, worker=worker, user=user)
        if choice in {"7", "pool", "pool-mine", "randomx"}:
            return_code = run_randomx_pool_prompt(args)
            if return_code not in {0, 1, 2}:
                return return_code
            continue
        if choice in {"8", "mine", "mine-block"}:
            seed = sha256((args.seed or "CYTHANX-PROMPT").encode("utf-8"))
            payload = cythanize(seed)["commitment"]
            block = Block(0, int(time.time()), "00" * 32, payload)
            found = block.mine(
                max_nonce=args.max_nonce,
                alien_string=args.alien_string,
                alien_gate_bits=args.alien_gate_bits,
                dragon_enigma_key=bytes.fromhex(args.dragon_enigma_key) if args.dragon_enigma_key else None,
                dragon_gate_bits=args.dragon_gate_bits,
            )
            if found:
                ledger = ShareLedger(args.ledger_file)
                record = ledger.append_share(
                    block, source="prompt-mine",
                    alien_string=args.alien_string,
                    alien_gate_bits=args.alien_gate_bits,
                    dragon_enigma_key=bytes.fromhex(args.dragon_enigma_key) if args.dragon_enigma_key else None,
                    dragon_gate_bits=args.dragon_gate_bits,
                )
                print(json.dumps({"block": asdict(block), "ledger_record": record,
                                  "ledger": ledger.summary()}, indent=2))
            else:
                print(f"[MINING] bounded window exhausted at nonce {args.max_nonce}")
            continue
        if choice in {"9", "status"}:
            print(json.dumps(mog_status(), indent=2))
            continue
        print("[PROMPT] Choose 1-9 or q.")


def self_test() -> dict:
    sample = b"CYTHANX|SELF-TEST"
    d = sha256d(sample)
    c = cythanize(sample)
    j = joatt(sample)
    o = organism(d, 2)
    block = Block(0, 0, "00"*32, c["commitment"])
    # Avoid an expensive proof-of-work test: verify deterministic 32-byte output.
    header = block.header_bytes()
    digest = block.layered_hash()
    gate_digest = alien_gate_digest(header)
    de_digest = dragon_enigma_digest(
        header, block.nonce, b"CYTHANX-SELF-TEST-DRAGON-ENIGMA"
    )
    assert len(de_digest) == 32
    assert 0.0 <= dragon_trend(de_digest) <= 1.0
    assert dragon_phase(dragon_trend(de_digest)) in {
        "QUIET", "RISING", "OSCILLATING", "TURBULENT", "DRAGON"
    }
    assert len(header) == HEADER_SIZE == 80
    assert len(gate_digest) == 32
    assert isinstance(alien_gate_passes(header), bool)
    assert len(d) == len(digest) == 32
    assert len(bytes.fromhex(j["state256"])) == 32
    assert o["organism"]["generation"] == 2
    core_a, root_a = cyx_core_string(b"CYTHANX", b"CORE", sample)
    core_b, root_b = cyx_core_string(b"CYTHANX", b"CORE", sample)
    assert core_a == core_b and root_a == root_b
    assert (b"|ROOT|" + root_a.hex().encode()) in core_a
    genesis_ok, genesis, genesis_reason = verify_genesis()
    assert genesis_ok, genesis_reason
    return {
        "ok": True, "sha256d": d.hex(),
        "cythan_commitment": c["commitment"],
        "joatt_state256": j["state256"],
        "layered_block_hash": digest.hex(),
        "alien_gate_digest": gate_digest.hex(),
        "alien_gate_bits": ALIEN_GATE_BITS,
        "alien_gate_passed_for_self_test_header": alien_gate_passes(header),
        "dragon_enigma_digest": de_digest.hex(),
        "dragon_enigma_trend": dragon_trend(de_digest),
        "dragon_enigma_phase": dragon_phase(dragon_trend(de_digest)),
        "header_size": len(header),
        "genesis_verified": genesis_ok,
        "genesis_layered_hash": genesis["layered_hash"],
        "blake3_backend": "blake3" if _blake3 else "blake2b-fallback"
    }

def parse_args():
    p = argparse.ArgumentParser(description="CYTHANX MOG strategic fusion engine and operating controller")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--fuse", nargs="*", type=Path)
    p.add_argument("--seed")
    p.add_argument("--organism", action="store_true")
    p.add_argument("--mine-block", action="store_true")
    p.add_argument("--dragonfly", action="store_true", help="run the complete CYTHANX Dragonfly organism-of-work miner in one operation")
    p.add_argument("--max-nonce", type=int, default=1000, help="maximum nonce for a bounded mining/debug run")
    p.add_argument("--alien-string", default=ALIEN_GATE_STRING, help="domain-separator string for the CYTHANX alien gate")
    p.add_argument("--alien-gate-bits", type=int, default=ALIEN_GATE_BITS, help="leading zero bits required by the alien gate (0 disables the gate)")
    p.add_argument("--dragon-enigma-key", help="hex key enabling the optional Dragon-Enigma gate")
    p.add_argument("--dragon-gate-bits", type=int, default=0, help="Dragon lower-field gate bits; 0 disables it")
    p.add_argument("--ledger-file", type=Path, default=LEDGER_FILE, help="persistent CYTHANX share-ledger JSONL path")
    p.add_argument("--organism-rounds", type=int, default=3, help="CYTHANX organism rounds")
    p.add_argument("--dragonfly-rounds", type=int, default=3, help="Dragonfly organism rounds")
    p.add_argument("--bip39-wordlist", type=Path, help="path to the official 2048-word BIP-39 English list")
    p.add_argument("--bip39-generate", action="store_true", help="generate a secure 24-word BIP-39 mnemonic and derive BIP-32/BIP-44 data")
    p.add_argument("--bip39-mnemonic", help="derive from an explicitly supplied BIP-39 mnemonic (avoid shell history for real secrets)")
    p.add_argument("--bip39-passphrase", default="", help="BIP-39 passphrase; use only for controlled/testing workflows")
    p.add_argument("--bip39-path", default="m/44'/0'/0'/0/0", help="BIP-32/BIP-44 derivation path")
    p.add_argument("--bip39-fusion", action="store_true", help="feed the BIP-39 seed through the existing CYTHANX JOATT/CYTHANIZE fusion")
    p.add_argument(
        "--procedure",
        choices=("validate", "fuse", "organism", "mine-block", "genesis-verify", "ledger-verify", "dragonfly"),
        help="explicit operating procedure; equivalent to the corresponding mode",
    )
    p.add_argument("--procedures", action="store_true", help="print the operating-procedure allocation and exit")
    p.add_argument("--mog-status", action="store_true", help="print CYTHANX MOG structural status and exit")
    p.add_argument("--all", action="store_true", help="execute the complete procedure chain in one invocation")
    return p.parse_args()

def print_operating_procedures() -> None:
    procedures = {
        "validate": "Run deterministic self-test; do not start a miner.",
        "fuse": "Digest and fuse supplied source files into a fusion commitment.",
        "organism": "Derive CYTHANX organism state from the selected seed.",
        "mine-block": "Construct, mine, and persist an accepted CYTHANX share to the local ledger.",
        "genesis-verify": "Recompute and verify the deterministic CYTHANX genesis fingerprint.",
        "ledger-verify": "Verify the append-only share ledger, hash chain, genesis anchor, and JOATT chain.",
        "dragonfly": "Run genesis verification, CYTHANX organism work, JOATT conformance, native PoW/gates, share persistence, and post-mining ledger verification as one operation.",
    }
    print(json.dumps({"operating_procedures": procedures}, indent=2))




def run_all_procedures(args: argparse.Namespace) -> int:
    """Execute the native CYTHANX procedure chain without external miners."""
    import subprocess
    import sys
    commands = [
        [sys.executable, __file__, "--self-test"],
        [sys.executable, __file__, "--fuse", __file__],
        [sys.executable, __file__, "--organism", "--organism-rounds", str(max(1, args.organism_rounds))],
        [sys.executable, __file__, "--mine-block", "--max-nonce", str(args.max_nonce)],
    ]
    for i, command in enumerate(commands, 1):
        print(f"[{i}/4] {' '.join(command[2:])}")
        result = subprocess.run(command)
        if result.returncode != 0:
            if i == 4 and args.max_nonce is not None:
                print("[4/4] bounded mining window exhausted")
            else:
                return result.returncode
    print("[CYTHANX] native procedure chain complete")
    return 0

def main() -> int:
    args = parse_args()
    if len(os.sys.argv) == 1:
        return automated_setup_prompt(args)
    if args.procedures:
        print_operating_procedures()
        return 0

    if args.mog_status:
        print(json.dumps(mog_status(), indent=2))
        return 0

    if args.bip39_generate or args.bip39_mnemonic or args.bip39_fusion:
        if not args.bip39_wordlist:
            raise SystemExit("--bip39-wordlist is required for BIP-39 modes")
        try:
            words = load_bip39_wordlist(args.bip39_wordlist)
            if args.bip39_generate:
                report = bip39_generate_report(words, args.bip39_passphrase, args.bip39_path)
                print(json.dumps(report, indent=2))
                return 0
            if not args.bip39_mnemonic:
                raise SystemExit("--bip39-mnemonic is required for --bip39-fusion")
            report = bip39_derive_report(args.bip39_mnemonic, args.bip39_passphrase,
                                         words, args.bip39_path)
            if args.bip39_fusion:
                report["cythanx_fusion"] = mnemonic_fusion(args.bip39_mnemonic,
                                                           args.bip39_passphrase, words)
            print(json.dumps(report, indent=2))
            return 0
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"[BIP39] {exc}")
            return 2

    if args.dragonfly:
        return run_dragonfly(args)

    if args.all:
        return run_all_procedures(args)

    # Explicit operating-procedure allocation. Existing flags remain supported
    # for backward compatibility; --procedure is the unambiguous SOP interface.
    if args.procedure:
        return run_mog(args)

    if args.self_test:
        print(json.dumps(self_test(), indent=2))
        return 0

    if args.fuse is not None:
        result = fuse_files(args.fuse, args.seed)
        print(json.dumps(result, indent=2))
        seed = bytes.fromhex(result["fusion_commitment"])
    else:
        seed = sha256((args.seed or "CYTHANX").encode())

    if args.organism:
        print(json.dumps(organism(seed), indent=2))

    if args.mine_block:
        payload = cythanize(seed)["commitment"]
        block = Block(0, int(time.time()), "00"*32, payload)
        if block.mine(
            max_nonce=args.max_nonce,
            alien_string=args.alien_string,
            alien_gate_bits=args.alien_gate_bits,
            dragon_enigma_key=bytes.fromhex(args.dragon_enigma_key) if args.dragon_enigma_key else None,
            dragon_gate_bits=args.dragon_gate_bits,
        ):
            result = asdict(block)
            result["alien_gate"] = {
                "string": args.alien_string,
                "bits": args.alien_gate_bits,
                "digest": alien_gate_digest(block.header_bytes(), args.alien_string).hex(),
                "passed": True,
            }
            print(json.dumps(result, indent=2))
        else:
            print("Block mining stopped: no candidate satisfied both PoW and the alien string gate in the bounded nonce window.")
            return 1

    if args.fuse is None and not args.organism and not args.mine_block:
        print(json.dumps(self_test(), indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
