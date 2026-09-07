// LockInLedger Assistant — v6
// =============================================================================
// One surface: a chat. There are no forms.
//
// Everything the operator can do is typed:
//
//     Paid $450 to Handy Fix LLC for Repair and Maintenance from Bank of
//       America 9523 on 06/04/2026
//     show 260902000001
//     update 260902000001 amount 500, category Printing
//     void 260902000001
//     add vendor Handy Fix LLC, email ops@handyfix.com
//
// The `mode` selector is a hint sent alongside the message, not a form switch.
// It only disambiguates an otherwise bare line (a lone voucher id); every
// command works in every mode.
//
// Structured results arrive as `data.card` ({ kind: 'voucher' | 'profile' })
// and are rendered as record cards beneath the reply text.
// =============================================================================

import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import {
  Box, Paper, Typography, TextField, IconButton, CircularProgress, Divider,
  ThemeProvider, createTheme, CssBaseline, Snackbar, Alert, Tooltip, Drawer,
  ToggleButton, ToggleButtonGroup, Button, Collapse, Autocomplete,
} from '@mui/material';
import ArrowUpwardIcon from '@mui/icons-material/ArrowUpward';
import RefreshIcon from '@mui/icons-material/Refresh';
import CloseIcon from '@mui/icons-material/Close';
import ContentCopyIcon from '@mui/icons-material/ContentCopy';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import CheckIcon from '@mui/icons-material/Check';
import AddIcon from '@mui/icons-material/Add';
import EditIcon from '@mui/icons-material/Edit';
import PersonAddIcon from '@mui/icons-material/PersonAdd';
import AccountTreeIcon from '@mui/icons-material/AccountTree';
import HistoryIcon from '@mui/icons-material/History';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutlined';
import LightbulbOutlinedIcon from '@mui/icons-material/LightbulbOutlined';
import axios from 'axios';

// -----------------------------------------------------------------------------
// Config
// -----------------------------------------------------------------------------
// Bundler-agnostic on purpose. `process.env` does not exist in a Vite bundle and
// referencing it throws at module load, which blanks the whole app. Set the URL
// from index.html when it differs from the default:
//     <script>window.__API_BASE_URL__ = "https://ledger.internal:8000";</script>
const API_BASE_URL =
  (typeof window !== 'undefined' && window.__API_BASE_URL__) || 'http://localhost:8000';

// -----------------------------------------------------------------------------
// Design tokens — a neutral enterprise palette, one accent, no gradients.
// -----------------------------------------------------------------------------
const C = {
  bg: '#F7F8FA',
  surface: '#FFFFFF',
  raised: '#FBFCFD',
  line: '#E4E7EC',
  lineStrong: '#D0D5DD',
  ink: '#101828',
  inkMid: '#475467',
  inkMute: '#8A94A6',
  accent: '#1B3A6B',
  accentSoft: '#EEF2F8',
  ok: '#067647',
  okSoft: '#ECFDF3',
  warn: '#B54708',
  warnSoft: '#FFFAEB',
  err: '#B42318',
  errSoft: '#FEF3F2',
  violet: '#5B3FA8',
  violetSoft: '#F5F2FE',
  blue: '#1D6FB8',
  blueSoft: '#EFF6FC',
};

const MONO = '"SFMono-Regular", ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace';
const SANS = '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';

const theme = createTheme({
  palette: {
    mode: 'light',
    primary: { main: C.accent },
    background: { default: C.bg, paper: C.surface },
    text: { primary: C.ink, secondary: C.inkMid },
    divider: C.line,
  },
  typography: {
    fontFamily: SANS,
    h6: { fontSize: 15, fontWeight: 600, letterSpacing: '-0.01em' },
    body1: { fontSize: 14, lineHeight: 1.6 },
    body2: { fontSize: 13, lineHeight: 1.55 },
    caption: { fontSize: 11.5, letterSpacing: '0.02em' },
    button: { textTransform: 'none', fontWeight: 500 },
  },
  shape: { borderRadius: 6 },
  components: {
    MuiPaper: { defaultProps: { elevation: 0 } },
    MuiTooltip: {
      styleOverrides: {
        tooltip: { backgroundColor: C.ink, fontSize: 11.5, fontWeight: 400, borderRadius: 4 },
      },
    },
  },
});

// -----------------------------------------------------------------------------
// Formatting
// -----------------------------------------------------------------------------
const money = (v) =>
  Number(v ?? 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const formatDateForDisplay = (iso) => {
  if (!iso) return '';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso));
  return m ? `${m[2]}/${m[3]}/${m[1]}` : String(iso);
};

const clockTime = (d) =>
  new Date(d).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });

// -----------------------------------------------------------------------------
// Primitives
// -----------------------------------------------------------------------------
const Mono = ({ children, sx, ...p }) => (
  <Box component="span" sx={{ fontFamily: MONO, fontSize: 12.5, letterSpacing: '-0.01em', ...sx }} {...p}>
    {children}
  </Box>
);

const Pill = ({ label, tone = 'neutral' }) => {
  const tones = {
    neutral: { bg: '#F2F4F7', fg: C.inkMid, bd: C.line },
    accent: { bg: C.accentSoft, fg: C.accent, bd: '#D6E0EF' },
    ok: { bg: C.okSoft, fg: C.ok, bd: '#ABEFC6' },
    warn: { bg: C.warnSoft, fg: C.warn, bd: '#FEDF89' },
    err: { bg: C.errSoft, fg: C.err, bd: '#FECDCA' },
  };
  const t = tones[tone] || tones.neutral;
  return (
    <Box
      component="span"
      sx={{
        display: 'inline-flex', alignItems: 'center', height: 20, px: 0.75,
        borderRadius: '4px', border: `1px solid ${t.bd}`, background: t.bg, color: t.fg,
        fontSize: 11, fontWeight: 600, letterSpacing: '0.02em', whiteSpace: 'nowrap',
      }}
    >
      {label}
    </Box>
  );
};

// A definition row inside a record card.
const Row = ({ label, children, mono }) =>
  children === null || children === undefined || children === '' ? null : (
    <Box
      sx={{
        display: 'grid', gridTemplateColumns: { xs: '110px 1fr', sm: '140px 1fr' },
        gap: 1.5, py: 0.75, borderBottom: `1px solid ${C.line}`,
        '&:last-of-type': { borderBottom: 'none' },
      }}
    >
      <Typography sx={{ fontSize: 12.5, color: C.inkMute }}>{label}</Typography>
      <Box sx={{ fontSize: 13, color: C.ink, fontFamily: mono ? MONO : 'inherit', minWidth: 0, wordBreak: 'break-word' }}>
        {children}
      </Box>
    </Box>
  );

const CopyButton = ({ value, title = 'Copy' }) => (
  <Tooltip title={title}>
    <IconButton
      size="small"
      onClick={() => navigator.clipboard?.writeText(String(value))}
      sx={{ width: 22, height: 22, color: C.inkMute, '&:hover': { color: C.ink } }}
    >
      <ContentCopyIcon sx={{ fontSize: 13 }} />
    </IconButton>
  </Tooltip>
);

// -----------------------------------------------------------------------------
// Record cards
// -----------------------------------------------------------------------------
const CardShell = ({ title, right, children, tone = 'neutral' }) => (
  <Paper
    sx={{
      mt: 1.5, border: `1px solid ${C.line}`, borderRadius: '8px',
      background: C.surface, overflow: 'hidden',
    }}
  >
    <Box
      sx={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1,
        px: 1.75, py: 1.1, background: C.raised, borderBottom: `1px solid ${C.line}`,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, minWidth: 0 }}>
        <Box
          sx={{
            width: 3, height: 14, borderRadius: 2, flexShrink: 0,
            background: tone === 'err' ? C.err : tone === 'warn' ? C.warn : C.accent,
          }}
        />
        {title}
      </Box>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexShrink: 0 }}>{right}</Box>
    </Box>
    <Box sx={{ px: 1.75, py: 1 }}>{children}</Box>
  </Paper>
);

// A voucher, as a document rather than a paragraph.
//
// One component serves every place a voucher appears — freshly posted, looked
// up with `show`, updated, voided — because they are the same record and
// should not look like three different things. The two shapes it is handed
// (the flat payload of a fresh post, the `card` of a lookup) are normalised
// first; a fresh post has no legs stored yet, so its journal entry is derived
// from the direction, which is the one place polarity is defined client-side.
const ENTRY_TONE = {
  CRV: { bar: '#065F46', soft: C.okSoft, edge: '#A7D8C0',
         doc: 'Cash Receipt Voucher', flow: 'Money in', party: 'Customer',
         category: 'Income account' },
  CPV: { bar: C.accent, soft: C.accentSoft, edge: '#D6E0EF',
         doc: 'Cash Payment Voucher', flow: 'Money out', party: 'Vendor',
         category: 'Expense account' },
};

const normalizeVoucher = (v) => {
  const entryType = v.entry_type === 'CRV' ? 'CRV' : 'CPV';
  const amount = Number(v.amount ?? 0);
  const bank = { code: v.bank_acc_code, name: v.bank_account };
  const cat = { code: v.category_acc_code, name: v.category_account };
  // A lookup carries its real legs; a fresh post carries none, so derive them.
  const legs = v.legs?.length
    ? v.legs
    : (bank.code && cat.code
        ? (entryType === 'CRV'
            ? [{ sno: 1, code: bank.code, account: bank.name, dc: 'D', amount },
               { sno: 2, code: cat.code, account: cat.name, dc: 'C', amount: -amount }]
            : [{ sno: 1, code: cat.code, account: cat.name, dc: 'D', amount },
               { sno: 2, code: bank.code, account: bank.name, dc: 'C', amount: -amount }])
        : []);
  const status = v.at_status === undefined || v.at_status === null ? '1' : String(v.at_status);
  return {
    entryType, amount, bank, cat, legs, status,
    atId: v.at_id,
    number: v.voucher_number,
    date: v.transaction_date,
    party: v.party_name,
    partyCode: v.party_code,
    cheque: v.cheque_no,
    description: v.description,
    statusLabel: v.status_label || { '0': 'Void', '1': 'Posted', '2': 'In ledger' }[status] || status,
    editable: v.editable !== false && status === '1',
    blockers: v.blockers || [],
    // `note` on a lookup, `review_note` on a fresh post — same meaning.
    note: v.note || v.review_note || null,
  };
};

// label + value, numbered so the eye can travel down the document
const VoucherRow = ({ n, label, value, sub, mono, last }) => (
  <Box
    sx={{
      display: 'grid', gridTemplateColumns: '22px 1fr auto', gap: 1.25,
      alignItems: 'start', px: 1.75, py: 0.95,
      borderBottom: last ? 'none' : `1px solid ${C.line}`,
    }}
  >
    <Box
      sx={{
        width: 18, height: 18, borderRadius: '50%', background: C.raised,
        border: `1px solid ${C.line}`, color: C.inkMute, fontSize: 10,
        display: 'grid', placeItems: 'center', mt: '-1px',
      }}
    >
      {n}
    </Box>
    <Typography sx={{ fontSize: 12, fontWeight: 600, color: C.inkMid,
                      letterSpacing: '0.01em' }}>
      {label}
    </Typography>
    <Box sx={{ textAlign: 'right', minWidth: 0, maxWidth: 300 }}>
      <Typography
        sx={{ fontSize: 13, color: value ? C.ink : C.inkMute,
              fontFamily: mono ? MONO : 'inherit', wordBreak: 'break-word' }}
      >
        {value || '—'}
      </Typography>
      {sub ? <Mono sx={{ fontSize: 10.5, color: C.inkMute }}>{sub}</Mono> : null}
    </Box>
  </Box>
);

const VoucherCard = ({ card, onCommand }) => {
  const [legsOpen, setLegsOpen] = useState(false);
  const v = useMemo(() => normalizeVoucher(card), [card]);
  const tone = ENTRY_TONE[v.entryType];
  const voidish = v.status === '0';
  const bar = voidish ? C.inkMid : tone.bar;

  return (
    <Paper
      sx={{
        mt: 1.5, border: `1px solid ${C.line}`, borderRadius: '10px',
        overflow: 'hidden', background: C.surface,
      }}
    >
      {/* Header — the whole result in one glance */}
      <Box sx={{ background: bar, color: '#fff', px: 1.75, py: 1.35 }}>
        <Box sx={{ display: 'flex', alignItems: 'flex-start',
                   justifyContent: 'space-between', gap: 1.5 }}>
          <Box sx={{ minWidth: 0 }}>
            <Typography sx={{ fontSize: 13.5, fontWeight: 600, letterSpacing: '-0.01em' }}>
              {tone.doc}
            </Typography>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mt: 0.25 }}>
              <Mono sx={{ fontSize: 11.5, opacity: 0.8 }}>{v.number}</Mono>
              <Tooltip title="Copy voucher id">
                <IconButton
                  size="small"
                  onClick={() => navigator.clipboard?.writeText(String(v.atId))}
                  sx={{ width: 18, height: 18, color: '#fff', opacity: 0.6,
                        '&:hover': { opacity: 1, background: 'rgba(255,255,255,0.15)' } }}
                >
                  <ContentCopyIcon sx={{ fontSize: 11 }} />
                </IconButton>
              </Tooltip>
              <Box sx={{ width: 3, height: 3, borderRadius: '50%', background: '#fff',
                         opacity: 0.4 }} />
              <Typography sx={{ fontSize: 11.5, opacity: 0.8 }}>
                {voidish ? 'Voided' : tone.flow}
              </Typography>
            </Box>
          </Box>
          <Box sx={{ textAlign: 'right', flexShrink: 0 }}>
            <Typography
              sx={{ fontFamily: MONO, fontSize: 21, fontWeight: 600,
                    letterSpacing: '-0.02em', lineHeight: 1.15,
                    textDecoration: voidish ? 'line-through' : 'none' }}
            >
              ${money(v.amount)}
            </Typography>
            <Box
              component="span"
              sx={{
                display: 'inline-block', mt: 0.4, px: 0.7, py: '1px',
                borderRadius: '4px', background: 'rgba(255,255,255,0.16)',
                fontSize: 10, fontWeight: 700, letterSpacing: '0.06em',
              }}
            >
              {String(v.statusLabel).toUpperCase()}
            </Box>
          </Box>
        </Box>
      </Box>

      {/* Anything matched automatically */}
      {v.note ? (
        <Box sx={{ px: 1.75, py: 1, background: C.warnSoft,
                   borderBottom: `1px solid #FEDF89` }}>
          <Typography sx={{ fontSize: 12, color: C.warn }}>
            <b>Please double-check:</b> {v.note}
          </Typography>
        </Box>
      ) : null}

      {!v.editable && v.blockers.length ? (
        <Box sx={{ px: 1.75, py: 1, background: C.errSoft,
                   borderBottom: `1px solid #FECDCA` }}>
          <Typography sx={{ fontSize: 12, color: C.err }}>
            <b>Not editable:</b> {v.blockers.join('; ')}.
          </Typography>
        </Box>
      ) : null}

      {/* The document */}
      <VoucherRow n={1} label="Date" value={formatDateForDisplay(v.date)} />
      <VoucherRow n={2} label={tone.party} value={v.party} sub={v.partyCode} />
      <VoucherRow n={3} label="Bank / cash" value={v.bank.name} sub={v.bank.code} />
      <VoucherRow n={4} label={tone.category} value={v.cat.name} sub={v.cat.code} />
      <VoucherRow n={5} label="Amount" value={`$${money(v.amount)}`} mono />
      <VoucherRow n={6} label="Reference" value={v.cheque ? `Cheque #${v.cheque}` : ''} mono />
      <VoucherRow n={7} label="Remarks" value={v.description} last />

      {/* Journal entry */}
      {v.legs.length ? (
        <Box sx={{ borderTop: `1px solid ${C.line}`, background: C.raised }}>
          <Box
            onClick={() => setLegsOpen((o) => !o)}
            sx={{ display: 'flex', alignItems: 'center', gap: 0.5, px: 1.75, py: 0.9,
                  cursor: 'pointer', '&:hover': { background: C.surface } }}
          >
            <KeyboardArrowDownIcon
              sx={{ fontSize: 16, color: C.inkMute, transition: '.15s',
                    transform: legsOpen ? 'none' : 'rotate(-90deg)' }}
            />
            <Typography sx={{ fontSize: 11.5, fontWeight: 600, color: C.inkMid,
                              letterSpacing: '0.04em', whiteSpace: 'nowrap' }}>
              JOURNAL ENTRY
            </Typography>
            {!legsOpen ? (
              <Mono
                sx={{ fontSize: 11, color: C.inkMute, ml: 0.5, minWidth: 0,
                      overflow: 'hidden', textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap' }}
              >
                Dr {v.legs.find((l) => l.dc === 'D')?.account} / Cr{' '}
                {v.legs.find((l) => l.dc === 'C')?.account}
              </Mono>
            ) : null}
          </Box>
          <Collapse in={legsOpen}>
            <Box sx={{ px: 1.75, pb: 1.25 }}>
              {v.legs.map((l, i) => (
                <Box
                  key={`${l.code}-${l.sno ?? i}`}
                  sx={{
                    display: 'grid', gridTemplateColumns: '26px 1fr auto', gap: 1,
                    alignItems: 'center', py: 0.7,
                    borderBottom: i < v.legs.length - 1 ? `1px solid ${C.line}` : 'none',
                  }}
                >
                  <Mono sx={{ fontWeight: 700, fontSize: 11,
                              color: l.dc === 'D' ? tone.bar : C.inkMid }}>
                    {l.dc === 'D' ? 'DR' : 'CR'}
                  </Mono>
                  <Box sx={{ minWidth: 0 }}>
                    <Typography sx={{ fontSize: 12.5, color: C.ink }} noWrap title={l.account}>
                      {l.account}
                    </Typography>
                    <Mono sx={{ fontSize: 10.5, color: C.inkMute }}>
                      {l.code}{l.nature ? ` · ${l.nature}` : ''}
                    </Mono>
                  </Box>
                  <Mono sx={{ fontSize: 12.5, color: Number(l.amount) < 0 ? C.inkMid : C.ink }}>
                    {Number(l.amount) < 0
                      ? `(${money(Math.abs(l.amount))})`
                      : money(l.amount)}
                  </Mono>
                </Box>
              ))}
            </Box>
          </Collapse>
        </Box>
      ) : null}

      {/* What you can do next */}
      {onCommand && !voidish ? (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75, px: 1.75, py: 1.25,
                   borderTop: `1px solid ${C.line}` }}>
          {(v.editable
            ? [['Change amount', `update ${v.atId} amount `],
               ['Change category', `update ${v.atId} category `],
               ['Change date', `update ${v.atId} date `],
               ['Void', `void ${v.atId}`]]
            : [['Open', `show ${v.atId}`], ['Void', `void ${v.atId}`]]
          ).map(([label, cmd]) => (
            <Button
              key={label}
              size="small"
              onClick={() => onCommand(cmd, cmd.endsWith(' '))}
              sx={{
                minHeight: 26, px: 1, fontSize: 12,
                color: label === 'Void' ? C.err : C.inkMid,
                border: `1px solid ${label === 'Void' ? '#FECDCA' : C.line}`,
                borderRadius: '5px',
                '&:hover': { borderColor: label === 'Void' ? C.err : C.lineStrong,
                             background: label === 'Void' ? C.errSoft : C.raised },
              }}
            >
              {label}
            </Button>
          ))}
        </Box>
      ) : null}
    </Paper>
  );
};

const ProfileCard = ({ card, onCommand }) => (
  <CardShell
    title={
      <Typography sx={{ fontSize: 13, fontWeight: 600, color: C.ink }} noWrap>
        {card.company_name || card.person_name}
      </Typography>
    }
    right={
      <>
        <Pill label={card.type_label} tone="accent" />
        <Pill label={card.created ? 'Created' : 'Existing'} tone={card.created ? 'ok' : 'neutral'} />
      </>
    }
  >
    <Row label="Party code" mono>{card.p_code}</Row>
    <Row label="Contact">{card.person_name && card.person_name !== card.company_name ? card.person_name : null}</Row>
    <Row label="Email">{card.email}</Row>
    <Row label="Phone" mono>{card.phone}</Row>
    <Row label="Address">{card.address}</Row>
    <Row label="Title">{card.job_title}</Row>
    <Row label="Default account" mono>{card.p_account}</Row>
    {onCommand ? (
      <Button
        size="small"
        onClick={() => onCommand(`Paid $0.00 to ${card.company_name || card.person_name} for `, true)}
        sx={{
          mt: 1.25, minHeight: 26, px: 1, fontSize: 12, color: C.inkMid,
          border: `1px solid ${C.line}`, borderRadius: '5px',
          '&:hover': { borderColor: C.lineStrong, background: C.raised },
        }}
      >
        Use in a voucher
      </Button>
    ) : null}
  </CardShell>
);

// A chart-of-accounts tree. Postable leaves are clickable — they drop the name
// into the composer, which is the whole point of looking the chart up.
const ChartCard = ({ card, onCommand }) => {
  const [open, setOpen] = useState(() => new Set());
  const toggle = (code) =>
    setOpen((prev) => {
      const next = new Set(prev);
      next.has(code) ? next.delete(code) : next.add(code);
      return next;
    });

  const Leaf = ({ name, code, indent }) => (
    <Box
      onClick={() => onCommand?.(name)}
      title={`${name}  ${code}`}
      sx={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        gap: 1, pl: indent, pr: 1, py: 0.5, borderRadius: '4px', cursor: 'pointer',
        '&:hover': { background: C.accentSoft },
      }}
    >
      <Typography sx={{ fontSize: 12.5, color: C.ink }} noWrap>{name}</Typography>
      <Mono sx={{ fontSize: 11, color: C.inkMute }}>{code}</Mono>
    </Box>
  );

  return (
    <CardShell
      title={
        <Typography sx={{ fontSize: 13, fontWeight: 600, color: C.ink }}>
          Chart of accounts{card.filtered_to ? ` — ${card.filtered_to}` : ''}
        </Typography>
      }
      right={<Pill label={`${card.postable_total} postable`} tone="accent" />}
    >
      {(card.natures || []).map((n) => (
        <Box key={n.nature} sx={{ mb: 1.25 }}>
          <Typography
            sx={{ fontSize: 11, fontWeight: 600, color: C.inkMute,
                  letterSpacing: '0.06em', mb: 0.4 }}
          >
            {n.label.toUpperCase()} · {n.statement}
          </Typography>
          {n.mains.map((m) =>
            m.postable ? (
              <Leaf key={m.code} name={m.name} code={m.code} indent={1} />
            ) : (
              <Box key={m.code}>
                <Box
                  onClick={() => toggle(m.code)}
                  sx={{
                    display: 'flex', alignItems: 'center', gap: 0.75, pl: 0.5, py: 0.5,
                    borderRadius: '4px', cursor: 'pointer',
                    '&:hover': { background: C.raised },
                  }}
                >
                  <KeyboardArrowDownIcon
                    sx={{ fontSize: 15, color: C.inkMute, transition: '.15s',
                          transform: open.has(m.code) ? 'none' : 'rotate(-90deg)' }}
                  />
                  <Typography sx={{ fontSize: 12.5, fontWeight: 500, color: C.inkMid }}>
                    {m.name}
                  </Typography>
                  <Mono sx={{ fontSize: 11, color: C.inkMute }}>{m.code}</Mono>
                  <Typography sx={{ fontSize: 11, color: C.inkMute }}>
                    · heading, {m.sub_count} below
                  </Typography>
                </Box>
                <Collapse in={open.has(m.code)}>
                  {m.subs.map((s) => (
                    <Leaf key={s.code} name={s.name} code={s.code} indent={3} />
                  ))}
                </Collapse>
              </Box>
            )
          )}
        </Box>
      ))}
      <Typography sx={{ fontSize: 11.5, color: C.inkMute, mt: 0.5 }}>
        Click any account to put its name in the composer. Headings have
        sub-accounts under them and cannot be posted to.
      </Typography>
    </CardShell>
  );
};

const AccountCard = ({ card, onCommand }) => (
  <CardShell
    tone="ok"
    title={
      <Typography sx={{ fontSize: 13, fontWeight: 600, color: C.ink }} noWrap>
        {card.name}
      </Typography>
    }
    right={
      <>
        <Pill label={card.level === 'sub' ? 'Sub-account' : 'Account'} tone="accent" />
        <Pill label="Created" tone="ok" />
      </>
    }
  >
    <Row label="Code" mono>{card.code}</Row>
    <Row label="Under">
      {card.parent_name}
      <Mono sx={{ color: C.inkMute, ml: 1 }}>{card.parent_code}</Mono>
    </Row>
    {onCommand ? (
      <Button
        size="small"
        onClick={() => onCommand(`Paid $0.00 to  for ${card.name}`, true)}
        sx={{
          mt: 1.25, minHeight: 26, px: 1, fontSize: 12, color: C.inkMid,
          border: `1px solid ${C.line}`, borderRadius: '5px',
          '&:hover': { borderColor: C.lineStrong, background: C.raised },
        }}
      >
        Use in a voucher
      </Button>
    ) : null}
  </CardShell>
);

// A day's vouchers, to pick from. Ticking is how a bulk edit starts: the
// selected ones open in the review panel one at a time, so a change to twenty
// vouchers is still twenty confirmations, not one blind sweep.
const VoucherListCard = ({ card, onBulkEdit, busy }) => {
  const editable = (card.vouchers || []).filter((v) => v.editable);
  const [picked, setPicked] = useState(() => new Set());
  const [instruction, setInstruction] = useState('');

  const toggle = (id) => setPicked((prev) => {
    const next = new Set(prev);
    next.has(id) ? next.delete(id) : next.add(id);
    return next;
  });
  const allOn = picked.size > 0 && picked.size === editable.length;

  return (
    <CardShell
      title={
        <Typography sx={{ fontSize: 13, fontWeight: 600, color: C.ink }}>
          Vouchers on {card.date_label}
        </Typography>
      }
      right={<Pill label={`${card.editable_count} editable`} tone="accent" />}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, pb: 0.75,
                 borderBottom: `1px solid ${C.line}` }}>
        <Button size="small"
          onClick={() => setPicked(allOn ? new Set() : new Set(editable.map((v) => v.at_id)))}
          sx={{ minHeight: 24, px: 0.75, fontSize: 11.5, color: C.inkMid }}
        >
          {allOn ? 'Clear all' : 'Select all editable'}
        </Button>
        <Typography sx={{ fontSize: 11.5, color: C.inkMute }}>
          {picked.size} selected
        </Typography>
      </Box>

      {(card.vouchers || []).map((v) => {
        const on = picked.has(v.at_id);
        return (
          <Box
            key={v.at_id}
            onClick={() => v.editable && toggle(v.at_id)}
            sx={{
              display: 'grid', gridTemplateColumns: '18px 1fr auto', gap: 1,
              alignItems: 'center', py: 0.85, borderBottom: `1px solid ${C.line}`,
              cursor: v.editable ? 'pointer' : 'default',
              opacity: v.editable ? 1 : 0.55,
              '&:hover': v.editable ? { background: C.raised } : {},
            }}
          >
            <Box sx={{ width: 14, height: 14, borderRadius: '3px',
                       border: `1.5px solid ${on ? C.accent : C.lineStrong}`,
                       background: on ? C.accent : 'transparent',
                       display: 'grid', placeItems: 'center' }}>
              {on ? <CheckIcon sx={{ fontSize: 11, color: '#fff' }} /> : null}
            </Box>
            <Box sx={{ minWidth: 0 }}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                <Mono sx={{ fontSize: 11.5, color: C.inkMid }}>{v.voucher_number}</Mono>
                {!v.editable ? <Pill label={v.status_label} tone="warn" /> : null}
              </Box>
              <Typography sx={{ fontSize: 12, color: C.inkMute }} noWrap>
                {v.party_name || '—'}{v.description ? ` · ${v.description}` : ''}
              </Typography>
            </Box>
            <Mono sx={{ fontSize: 13, fontWeight: 600, color: C.ink }}>
              ${money(v.amount)}
            </Mono>
          </Box>
        );
      })}

      <Box sx={{ mt: 1.25 }}>
        <Typography sx={{ fontSize: 10.5, fontWeight: 700, letterSpacing: '0.06em',
                          color: C.inkMute, mb: 0.5 }}>
          CHANGE TO APPLY (OPTIONAL)
        </Typography>
        <TextField
          fullWidth size="small" placeholder="amount 500, category Printing"
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          sx={{ '& .MuiOutlinedInput-root': { fontSize: 12.5, background: C.raised,
                  borderRadius: '6px',
                  '& fieldset': { borderColor: C.line } } }}
        />
        <Typography sx={{ fontSize: 11, color: C.inkMute, mt: 0.5 }}>
          Applied to every one you picked, then shown for confirmation one at a time.
        </Typography>
        <Button
          fullWidth disableElevation variant="contained"
          disabled={!picked.size || busy}
          onClick={() => onBulkEdit([...picked], instruction.trim())}
          sx={{ mt: 1, py: 0.75, fontSize: 12.5, fontWeight: 600, borderRadius: '6px',
                background: C.accent, '&:hover': { background: '#16304F' } }}
        >
          {busy ? 'Opening…'
                : `Review ${picked.size || ''} voucher${picked.size === 1 ? '' : 's'}`.replace('  ', ' ')}
        </Button>
      </Box>
    </CardShell>
  );
};

// -----------------------------------------------------------------------------
// Review panel — the confirmation step between typing and posting.
//
// The backend resolved the sentence into codes but wrote nothing. Everything
// here is editable, because the whole point is to catch the cases where the
// parser guessed wrong. Accounts are chosen from the real chart rather than
// typed, so a correction always lands on a code that exists.
// -----------------------------------------------------------------------------
const DRAFT_INPUT_SX = {
  // No `InputProps`/`slotProps` anywhere below: the two spellings belong to
  // different MUI majors, and styling the underline away works on both.
  '& .MuiInput-root:before, & .MuiInput-root:after': { display: 'none' },
  '& .MuiInputBase-root': {
    fontSize: 14, color: C.ink, px: 0.75, py: 0.25, borderRadius: '5px',
    background: 'transparent', transition: 'background .12s',
    '&:hover': { background: C.raised },
    '&.Mui-focused': { background: C.accentSoft },
  },
  '& .MuiInputBase-input': { p: 0 },
};

const DraftField = ({ label, children, hint, tone }) => (
  <Box sx={{ px: 2, py: 1.1, borderBottom: `1px solid ${C.line}` }}>
    <Typography
      sx={{ fontSize: 10.5, fontWeight: 700, letterSpacing: '0.08em',
            color: C.inkMute, mb: 0.5 }}
    >
      {label}
    </Typography>
    {children}
    {hint ? (
      <Typography sx={{ fontSize: 11, mt: 0.4, color: tone === 'warn' ? C.warn : C.inkMute }}>
        {hint}
      </Typography>
    ) : null}
  </Box>
);

// Chart accounts as Autocomplete options.
const toAccountOptions = (rows) =>
  (rows || []).map((r) => ({ code: String(r.code), label: r.qualified || r.name, name: r.name }));

// A profile draft. Same review step as a voucher: the parser filled these in,
// the operator corrects them, nothing is written until Create.
const PartyDraftFields = ({ draft, set, pickers }) => {
  const accOptions = useMemo(() => toAccountOptions(pickers.all || pickers.bank),
                             [pickers.all, pickers.bank]);
  const text = (key, label, extra = {}) => (
    <DraftField key={key} label={label}>
      <TextField
        fullWidth variant="standard" placeholder="—"
        value={draft[key] || ''}
        onChange={(e) => set({ [key]: e.target.value })}
        sx={DRAFT_INPUT_SX} {...extra}
      />
    </DraftField>
  );

  return (
    <>
      <DraftField label="KIND" hint="Decides which control account the code is seeded from">
        <ToggleButtonGroup
          exclusive size="small" value={draft.p_type}
          onChange={(e, v) => v && set({
            p_type: v,
            type_label: (draft.p_types || []).find((t) => t.value === v)?.label || v,
          })}
          sx={{ flexWrap: 'wrap',
                '& .MuiToggleButton-root': {
                  px: 1, py: 0.3, fontSize: 11.5, color: C.inkMid, borderColor: C.line,
                  '&.Mui-selected': { background: C.blueSoft, color: C.blue,
                                      borderColor: '#CBE2F4',
                                      '&:hover': { background: C.blueSoft } } } }}
        >
          {(draft.p_types || []).map((t) => (
            <ToggleButton key={t.value} value={t.value}>{t.label}</ToggleButton>
          ))}
        </ToggleButtonGroup>
      </DraftField>

      <DraftField
        label="NAME"
        tone={draft.existing ? 'warn' : undefined}
        hint={draft.existing
          ? `Already exists as ${draft.existing.p_code} — it will be reused, not duplicated.`
          : undefined}
      >
        <TextField
          fullWidth variant="standard" placeholder="Company or person"
          value={draft.company_name || ''}
          onChange={(e) => set({ company_name: e.target.value })}
          sx={DRAFT_INPUT_SX}
        />
      </DraftField>

      {text('person_name', 'CONTACT PERSON')}
      {text('email', 'EMAIL')}
      {text('phone', 'PHONE')}
      {text('address', 'ADDRESS')}
      {text('city', 'CITY')}
      {text('state', 'STATE')}
      {text('zipcode', 'ZIP')}
      {text('job_title', 'JOB TITLE')}

      <DraftField label="DEFAULT ACCOUNT"
                  hint={draft.p_account ? `Code ${draft.p_account}` : 'Optional'}>
        <Autocomplete
          options={accOptions}
          value={accOptions.find((o) => o.code === String(draft.p_account)) || null}
          onChange={(e, v) => set({ p_account: v?.code || '', p_account_label: v?.label || '' })}
          isOptionEqualToValue={(o, v) => o.code === v?.code}
          getOptionLabel={(o) => o?.label || ''}
          renderInput={(params) => (
            <TextField {...params} variant="standard" placeholder="—" sx={DRAFT_INPUT_SX} />
          )}
        />
      </DraftField>

      {text('other_desc', 'NOTES')}
    </>
  );
};

// A chart-account draft. The parent is picked from the real chart, so a
// correction always lands somewhere that exists.
const AccountDraftFields = ({ draft, set }) => {
  const options = draft.parent_options || [];
  const current = options.find(
    (o) => o.code === String(draft.parent_code) && o.level === draft.level) || null;

  return (
    <>
      <DraftField label="ACCOUNT NAME">
        <TextField
          fullWidth variant="standard" placeholder="Name"
          value={draft.name || ''}
          onChange={(e) => set({ name: e.target.value })}
          sx={DRAFT_INPUT_SX}
        />
      </DraftField>

      <DraftField
        label="GOES UNDER"
        hint={draft.level === 'sub'
          ? `Sub-account of ${draft.parent_name} [${draft.parent_code}]`
          : `Main account directly under ${draft.parent_name}`}
      >
        <Autocomplete
          options={options}
          groupBy={(o) => o.group}
          value={current}
          onChange={(e, v) => set({
            parent_code: v?.code || '',
            parent_name: v?.label || '',
            level: v?.level || draft.level,
            level_label: v?.level === 'main' ? 'Main account' : 'Sub-account',
          })}
          isOptionEqualToValue={(o, v) => o.code === v?.code && o.level === v?.level}
          getOptionLabel={(o) => o?.label || ''}
          renderInput={(params) => (
            <TextField {...params} variant="standard" placeholder="Choose a parent"
                       sx={DRAFT_INPUT_SX} />
          )}
        />
      </DraftField>

      <DraftField label="LEVEL"
                  hint="Set by the parent — a nature makes a main, a main makes a sub">
        <Typography sx={{ fontSize: 14, color: C.ink, px: 0.75 }}>
          {draft.level_label || (draft.level === 'sub' ? 'Sub-account' : 'Main account')}
        </Typography>
      </DraftField>
    </>
  );
};

// An existing voucher opened for editing. Identical controls to a new one,
// with a "was …" line under anything the instruction (or the operator) changed
// — an edit is only reviewable if you can see what it is changing from.
const EditDraftFields = ({ draft, set, pickers, parties }) => {
  const isCRV = draft.entry_type === 'CRV';
  const bankOptions = useMemo(() => toAccountOptions(pickers.bank), [pickers.bank]);
  const catOptions = useMemo(
    () => toAccountOptions(isCRV ? pickers.income : pickers.expense),
    [pickers.income, pickers.expense, isCRV]);
  const partyOptions = useMemo(
    () => (parties || []).map((p) => ({
      code: String(p.p_code),
      label: p.company_name || p.person_name || String(p.p_code) })), [parties]);
  const o = draft.original || {};
  const findAcc = (opts, code) => opts.find((x) => x.code === String(code)) || null;

  // "was" only appears when the value actually differs from the stored one.
  const was = (key, shown) =>
    String(draft[key] ?? '') !== String(o[key] ?? '')
      ? `was ${shown || o[key] || '—'}` : undefined;

  return (
    <>
      <DraftField label="DATE" tone={was('transaction_date') ? 'warn' : undefined}
                  hint={was('transaction_date', formatDateForDisplay(o.transaction_date))}>
        <TextField fullWidth variant="standard" type="date"
          value={draft.transaction_date || ''}
          onChange={(e) => set({ transaction_date: e.target.value })}
          sx={DRAFT_INPUT_SX} />
      </DraftField>

      <DraftField label={isCRV ? 'RECEIVED FROM' : 'PAY TO'}
                  tone={was('party_code') ? 'warn' : undefined}
                  hint={was('party_code', o.party_name)}>
        <Autocomplete
          options={partyOptions}
          value={partyOptions.find((p) => p.code === String(draft.party_code)) || null}
          onChange={(e, v) => set({ party_code: v?.code || '', party_name: v?.label || '' })}
          isOptionEqualToValue={(a, b) => a.code === b?.code}
          getOptionLabel={(x) => x?.label || ''}
          renderInput={(params) => (
            <TextField {...params} variant="standard" placeholder="Choose a profile"
                       sx={DRAFT_INPUT_SX} />)} />
      </DraftField>

      <DraftField label="BANK / CASH" tone={was('bank_acc_code') ? 'warn' : undefined}
                  hint={was('bank_acc_code', o.bank_account)}>
        <Autocomplete options={bankOptions}
          value={findAcc(bankOptions, draft.bank_acc_code)}
          onChange={(e, v) => set({ bank_acc_code: v?.code || '', bank_account: v?.label || '' })}
          isOptionEqualToValue={(a, b) => a.code === b?.code}
          getOptionLabel={(x) => x?.label || ''}
          renderInput={(params) => (
            <TextField {...params} variant="standard" placeholder="Choose an account"
                       sx={DRAFT_INPUT_SX} />)} />
      </DraftField>

      <DraftField label={isCRV ? 'INCOME ACCOUNT' : 'EXPENSE ACCOUNT'}
                  tone={was('category_acc_code') ? 'warn' : undefined}
                  hint={was('category_acc_code', o.category_account)}>
        <Autocomplete options={catOptions}
          value={findAcc(catOptions, draft.category_acc_code)}
          onChange={(e, v) => set({ category_acc_code: v?.code || '',
                                    category_account: v?.label || '' })}
          isOptionEqualToValue={(a, b) => a.code === b?.code}
          getOptionLabel={(x) => x?.label || ''}
          renderInput={(params) => (
            <TextField {...params} variant="standard" placeholder="Choose an account"
                       sx={DRAFT_INPUT_SX} />)} />
      </DraftField>

      <DraftField label="AMOUNT" tone={was('amount') ? 'warn' : undefined}
                  hint={was('amount', o.amount != null ? `$${money(o.amount)}` : null)}>
        <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 0.5 }}>
          <Box sx={{ color: C.inkMute, fontFamily: MONO, fontSize: 15 }}>$</Box>
          <TextField fullWidth variant="standard" inputMode="decimal"
            value={draft.amount ?? ''}
            onChange={(e) => set({ amount: e.target.value })}
            sx={{ ...DRAFT_INPUT_SX,
                  '& .MuiInputBase-input': { p: 0, fontFamily: MONO, fontSize: 17,
                                             fontWeight: 600 } }} />
        </Box>
      </DraftField>

      <DraftField label="CHEQUE NO." tone={was('cheque_no') ? 'warn' : undefined}
                  hint={was('cheque_no')}>
        <TextField fullWidth variant="standard" placeholder="—"
          value={draft.cheque_no || ''}
          onChange={(e) => set({ cheque_no: e.target.value })}
          sx={{ ...DRAFT_INPUT_SX, '& .MuiInputBase-input': { p: 0, fontFamily: MONO } }} />
      </DraftField>

      <DraftField label="REMARKS" tone={was('description') ? 'warn' : undefined}
                  hint={was('description')}>
        <TextField fullWidth multiline maxRows={4} variant="standard" placeholder="—"
          value={draft.description || ''}
          onChange={(e) => set({ description: e.target.value })}
          sx={DRAFT_INPUT_SX} />
      </DraftField>
    </>
  );
};

const DraftPanel = ({ draft, setDraft, pickers, parties, onPost, onDiscard, posting,
                      error, queue }) => {
  const isCRV = draft.entry_type === 'CRV';
  const bankOptions = useMemo(() => toAccountOptions(pickers.bank), [pickers.bank]);
  const catOptions = useMemo(
    () => toAccountOptions(isCRV ? pickers.income : pickers.expense),
    [pickers.income, pickers.expense, isCRV]
  );
  const partyOptions = useMemo(
    () =>
      (parties || []).map((p) => ({
        code: String(p.p_code),
        label: p.company_name || p.person_name || String(p.p_code),
        type: p.p_type,
      })),
    [parties]
  );

  const set = (patch) => setDraft({ ...draft, ...patch });
  const findAcc = (opts, code) => opts.find((o) => o.code === String(code)) || null;

  // Switching direction re-points the category leg at a different set of
  // natures, so a category that was valid for a payment may not be valid for
  // a receipt. Drop it rather than posting to the wrong side of the ledger.
  const switchType = (next) => {
    if (!next || next === draft.entry_type) return;
    const nextCats = toAccountOptions(next === 'CRV' ? pickers.income : pickers.expense);
    const stillValid = nextCats.some((o) => o.code === String(draft.category_acc_code));
    setDraft({
      ...draft,
      entry_type: next,
      party_label: next === 'CRV' ? 'Customer' : 'Vendor',
      doc_label: next === 'CRV' ? 'Cash Receipt Voucher' : 'Cash Payment Voucher',
      category_acc_code: stillValid ? draft.category_acc_code : '',
      category_account: stillValid ? draft.category_account : '',
    });
  };

  // Three things can be drafted; each has its own required set, its own
  // colour and its own verb, but they share this one review step.
  const kind = draft.kind || 'voucher';
  const amountNum = Number(String(draft.amount ?? '').replace(/[^0-9.\-]/g, ''));
  const problems = [];
  if (kind === 'voucher') {
    if (!(amountNum > 0)) problems.push('an amount greater than zero');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(draft.transaction_date || ''))) problems.push('a date');
    if (!String(draft.party_name || '').trim()) problems.push(isCRV ? 'a customer' : 'a vendor');
    if (!draft.bank_acc_code) problems.push('a bank/cash account');
    if (!draft.category_acc_code) problems.push('a category account');
    if (draft.bank_acc_code && draft.bank_acc_code === draft.category_acc_code) {
      problems.push('two different accounts — both legs point at the same one');
    }
  } else if (kind === 'party') {
    if (!String(draft.company_name || '').trim()) problems.push('a name');
    if (!draft.p_type) problems.push('a kind');
  } else if (kind === 'account') {
    if (!String(draft.name || '').trim()) problems.push('an account name');
    if (!draft.parent_code) problems.push('a parent');
  } else if (kind === 'edit') {
    if (!(amountNum > 0)) problems.push('an amount greater than zero');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(draft.transaction_date || ''))) problems.push('a date');
    if (!draft.party_code) problems.push('a party');
    if (!draft.bank_acc_code) problems.push('a bank/cash account');
    if (!draft.category_acc_code) problems.push('a category account');
    if (draft.bank_acc_code && draft.bank_acc_code === draft.category_acc_code) {
      problems.push('two different accounts — both legs point at the same one');
    }
  }
  const ready = problems.length === 0;

  const SPEC = {
    voucher: { hue: isCRV ? '#065F46' : C.accent, verb: 'Post to LockInLedger',
               busy: 'Posting…', sub: 'Review & confirm before posting',
               foot: 'Nothing is written until you post.' },
    party:   { hue: C.blue, verb: 'Create profile', busy: 'Creating…',
               sub: 'Review & confirm before creating',
               foot: 'Nothing is written until you create it.' },
    account: { hue: C.violet, verb: 'Add to chart', busy: 'Creating…',
               sub: 'Review & confirm before creating',
               foot: 'Nothing is written until you create it.' },
    edit:    { hue: C.warn, verb: 'Save changes', busy: 'Saving…',
               sub: 'Review & confirm before saving',
               foot: 'Nothing changes until you save.' },
  }[kind];

  // Nothing edited yet is not an error — it just has nothing to save.
  const changed = kind !== 'edit' || ['amount', 'transaction_date', 'party_code',
    'bank_acc_code', 'category_acc_code', 'cheque_no', 'description'].some(
      (k) => String(draft[k] ?? '') !== String((draft.original || {})[k] ?? ''));

  const journal = isCRV
    ? `Dr ${draft.bank_account || '—'}  /  Cr ${draft.category_account || '—'}`
    : `Dr ${draft.category_account || '—'}  /  Cr ${draft.bank_account || '—'}`;

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0,
               background: C.surface }}>
      {/* Header */}
      <Box sx={{ px: 2, py: 1.5, background: SPEC.hue, color: '#fff', flexShrink: 0 }}>
        <Typography sx={{ fontSize: 14, fontWeight: 600, letterSpacing: '-0.01em' }}>
          {draft.doc_label || (isCRV ? 'Cash Receipt Voucher' : 'Cash Payment Voucher')}
        </Typography>
        <Typography sx={{ fontSize: 11.5, opacity: 0.75 }}>{SPEC.sub}</Typography>
      </Box>

      {/* Stepping through a bulk edit */}
      {queue && queue.total > 1 ? (
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                   px: 1, py: 0.6, background: C.raised,
                   borderBottom: `1px solid ${C.line}`, flexShrink: 0 }}>
          <IconButton size="small" disabled={queue.index === 0 || posting}
                      onClick={queue.onPrev} sx={{ color: C.inkMid }}>
            <KeyboardArrowDownIcon sx={{ fontSize: 18, transform: 'rotate(90deg)' }} />
          </IconButton>
          <Box sx={{ textAlign: 'center', minWidth: 0 }}>
            <Typography sx={{ fontSize: 11.5, fontWeight: 600, color: C.ink }}>
              {queue.index + 1} of {queue.total}
            </Typography>
            <Typography sx={{ fontSize: 10.5, color: C.inkMute }}>
              {queue.savedCount} saved · {queue.total - queue.savedCount} to go
            </Typography>
          </Box>
          <IconButton size="small" disabled={queue.index >= queue.total - 1 || posting}
                      onClick={queue.onNext} sx={{ color: C.inkMid }}>
            <KeyboardArrowDownIcon sx={{ fontSize: 18, transform: 'rotate(-90deg)' }} />
          </IconButton>
        </Box>
      ) : null}

      {/* Anything the parser guessed */}
      {draft.review_items?.length ? (
        <Box sx={{ px: 2, py: 1.1, background: C.warnSoft, borderBottom: `1px solid #FEDF89`,
                   flexShrink: 0 }}>
          <Typography sx={{ fontSize: 11.5, color: C.warn, fontWeight: 600, mb: 0.25 }}>
            Matched automatically — please check
          </Typography>
          <Typography sx={{ fontSize: 11.5, color: C.warn }}>
            {draft.review_items.join(' · ')}
          </Typography>
        </Box>
      ) : null}

      {/* Fields */}
      <Box sx={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
        {kind === 'party' ? <PartyDraftFields draft={draft} set={set} pickers={pickers} />
         : kind === 'account' ? <AccountDraftFields draft={draft} set={set} />
         : kind === 'edit' ? <EditDraftFields draft={draft} set={set} pickers={pickers}
                                              parties={parties} />
         : (
        <>
        <DraftField label="TYPE" hint={isCRV ? 'Money in — Dr bank, Cr revenue'
                                             : 'Money out — Dr expense, Cr bank'}>
          <ToggleButtonGroup
            exclusive
            size="small"
            value={draft.entry_type}
            onChange={(e, v) => switchType(v)}
            sx={{
              '& .MuiToggleButton-root': {
                px: 1.25, py: 0.3, fontSize: 12, color: C.inkMid, borderColor: C.line,
                '&.Mui-selected': { background: C.accentSoft, color: C.accent,
                                    borderColor: '#D6E0EF',
                                    '&:hover': { background: C.accentSoft } },
              },
            }}
          >
            <ToggleButton value="CRV">Receipt (CRV)</ToggleButton>
            <ToggleButton value="CPV">Payment (CPV)</ToggleButton>
          </ToggleButtonGroup>
        </DraftField>

        <DraftField label="DATE">
          <TextField
            fullWidth variant="standard" type="date"
            value={draft.transaction_date || ''}
            onChange={(e) => set({ transaction_date: e.target.value })}
            sx={DRAFT_INPUT_SX}
          />
        </DraftField>

        <DraftField
          label={isCRV ? 'RECEIVED FROM' : 'PAY TO'}
          tone={draft.party_is_new ? 'warn' : undefined}
          hint={draft.party_is_new
            ? 'Not in the ledger yet — a profile will be created when you post.'
            : (draft.party_code ? `Profile ${draft.party_code}` : undefined)}
        >
          <Autocomplete
            freeSolo
            options={partyOptions}
            value={draft.party_name || ''}
            onChange={(e, v) => {
              if (v && typeof v === 'object') {
                set({ party_code: v.code, party_name: v.label, party_is_new: false });
              } else {
                set({ party_code: null, party_name: v || '', party_is_new: !!v });
              }
            }}
            onInputChange={(e, v, reason) => {
              // Typing a name that is not an existing profile means "create it".
              if (reason === 'input') {
                const hit = partyOptions.find(
                  (o) => o.label.toLowerCase() === (v || '').trim().toLowerCase());
                set(hit
                  ? { party_code: hit.code, party_name: hit.label, party_is_new: false }
                  : { party_code: null, party_name: v, party_is_new: !!v });
              }
            }}
            getOptionLabel={(o) => (typeof o === 'string' ? o : o.label)}
            isOptionEqualToValue={(o, v) =>
              o.label === (typeof v === 'string' ? v : v?.label)}
            renderInput={(params) => (
              <TextField {...params} variant="standard" placeholder="Name"
                         sx={DRAFT_INPUT_SX} />
            )}
          />
        </DraftField>

        <DraftField label="BANK / CASH" hint={draft.bank_acc_code ? `Code ${draft.bank_acc_code}` : undefined}>
          <Autocomplete
            options={bankOptions}
            value={findAcc(bankOptions, draft.bank_acc_code)}
            onChange={(e, v) => set({ bank_acc_code: v?.code || '', bank_account: v?.label || '' })}
            isOptionEqualToValue={(o, v) => o.code === v?.code}
            getOptionLabel={(o) => o?.label || ''}
            renderInput={(params) => (
              <TextField {...params} variant="standard" placeholder="Choose an account"
                         sx={DRAFT_INPUT_SX} />
            )}
          />
        </DraftField>

        <DraftField
          label={isCRV ? 'INCOME ACCOUNT' : 'EXPENSE ACCOUNT'}
          tone={draft.category_matched === false ? 'warn' : undefined}
          hint={draft.category_matched === false
            ? 'Nothing in the message named this — a default was used.'
            : (draft.category_acc_code ? `Code ${draft.category_acc_code}` : undefined)}
        >
          <Autocomplete
            options={catOptions}
            value={findAcc(catOptions, draft.category_acc_code)}
            onChange={(e, v) => set({ category_acc_code: v?.code || '',
                                      category_account: v?.label || '',
                                      category_matched: true })}
            isOptionEqualToValue={(o, v) => o.code === v?.code}
            getOptionLabel={(o) => o?.label || ''}
            renderInput={(params) => (
              <TextField {...params} variant="standard" placeholder="Choose an account"
                         sx={DRAFT_INPUT_SX} />
            )}
          />
        </DraftField>

        <DraftField label="AMOUNT">
          <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 0.5 }}>
            <Box sx={{ color: C.inkMute, fontFamily: MONO, fontSize: 15 }}>$</Box>
            <TextField
              fullWidth variant="standard" inputMode="decimal"
              value={draft.amount ?? ''}
              onChange={(e) => set({ amount: e.target.value })}
              sx={{ ...DRAFT_INPUT_SX,
                    '& .MuiInputBase-input': { p: 0, fontFamily: MONO, fontSize: 17,
                                               fontWeight: 600 } }}
            />
          </Box>
        </DraftField>

        <DraftField label="CHEQUE NO.">
          <TextField
            fullWidth variant="standard" placeholder="—"
            value={draft.cheque_no || ''}
            onChange={(e) => set({ cheque_no: e.target.value })}
            sx={{ ...DRAFT_INPUT_SX, '& .MuiInputBase-input': { p: 0, fontFamily: MONO } }}
          />
        </DraftField>

        <DraftField label="REMARKS">
          <TextField
            fullWidth multiline maxRows={4} variant="standard" placeholder="—"
            value={draft.description || ''}
            onChange={(e) => set({ description: e.target.value })}
            sx={DRAFT_INPUT_SX}
          />
        </DraftField>

        <Box sx={{ px: 2, py: 1.25 }}>
          <Typography sx={{ fontSize: 10.5, fontWeight: 700, letterSpacing: '0.08em',
                            color: C.inkMute, mb: 0.5 }}>
            JOURNAL ENTRY
          </Typography>
          <Typography sx={{ fontFamily: MONO, fontSize: 11.5, color: C.inkMid,
                            lineHeight: 1.6 }}>
            {journal}
          </Typography>
        </Box>
        </>
        )}

        {draft.source_message ? (
          <Box sx={{ px: 2, py: 1.25 }}>
            <Typography sx={{ fontSize: 11, color: C.inkMute, fontStyle: 'italic' }}>
              From: “{draft.source_message}”
            </Typography>
          </Box>
        ) : null}
      </Box>

      {/* Footer */}
      <Box sx={{ flexShrink: 0, borderTop: `1px solid ${C.line}`, p: 1.5, background: C.raised }}>
        {error ? (
          <Typography sx={{ fontSize: 12, color: C.err, mb: 1 }}>{error}</Typography>
        ) : null}
        {!ready ? (
          <Typography sx={{ fontSize: 11.5, color: C.inkMute, mb: 1 }}>
            Needs {problems.join(', ')}.
          </Typography>
        ) : !changed ? (
          <Typography sx={{ fontSize: 11.5, color: C.inkMute, mb: 1 }}>
            Nothing changed yet — edit a field, or skip to the next.
          </Typography>
        ) : null}
        <Button
          fullWidth
          disableElevation
          variant="contained"
          disabled={!ready || !changed || posting}
          onClick={() => onPost((kind === 'voucher' || kind === 'edit')
            ? { ...draft, amount: amountNum } : draft)}
          startIcon={posting
            ? <CircularProgress size={13} thickness={5} sx={{ color: 'inherit' }} />
            : <CheckIcon sx={{ fontSize: 16 }} />}
          sx={{ py: 1, fontSize: 13.5, fontWeight: 600, borderRadius: '6px',
                background: SPEC.hue, '&:hover': { background: SPEC.hue, filter: 'brightness(1.12)' } }}
        >
          {posting ? SPEC.busy : SPEC.verb}
        </Button>
        <Button
          fullWidth
          disabled={posting}
          onClick={queue && queue.index < queue.total - 1 ? queue.onNext : onDiscard}
          sx={{ mt: 0.75, py: 0.75, fontSize: 12.5, color: C.inkMid,
                border: `1px solid ${C.line}`, borderRadius: '6px',
                '&:hover': { borderColor: C.lineStrong, background: C.surface } }}
        >
          {queue && queue.index < queue.total - 1 ? 'Skip to next' : 'Discard'}
        </Button>
        <Typography sx={{ mt: 0.75, fontSize: 10.5, color: C.inkMute, textAlign: 'center' }}>
          {SPEC.foot}
        </Typography>
      </Box>
    </Box>
  );
};

// -----------------------------------------------------------------------------
// Message
// -----------------------------------------------------------------------------
const Message = ({ msg, onCommand, onReopenDraft, onBulkEdit, busy }) => {
  if (msg.type === 'user') {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'flex-end', mb: 2 }}>
        <Box
          sx={{
            maxWidth: '80%', px: 1.5, py: 1, borderRadius: '8px 8px 2px 8px',
            background: C.accent, color: '#fff',
          }}
        >
          <Typography sx={{ fontSize: 13.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
            {msg.content}
          </Typography>
        </Box>
      </Box>
    );
  }

  return (
    <Box sx={{ mb: 2.5, maxWidth: 720 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.6 }}>
        <Typography sx={{ fontSize: 11.5, fontWeight: 600, color: C.inkMute, letterSpacing: '0.04em' }}>
          ASSISTANT
        </Typography>
        <Typography sx={{ fontSize: 11, color: C.inkMute }}>{clockTime(msg.timestamp)}</Typography>
        {msg.isError ? <Pill label="Not posted" tone="err" /> : null}
      </Box>
      <Paper
        sx={{
          px: 1.75, py: msg.content ? 1.35 : 1, pt: msg.content ? 1.35 : 0.5,
          border: `1px solid ${msg.isError ? '#FECDCA' : C.line}`,
          borderRadius: '8px', background: msg.isError ? C.errSoft : C.surface,
        }}
      >
        {msg.content ? (
          <Typography
            sx={{
              fontSize: 13.5, color: msg.isError ? C.err : C.ink,
              whiteSpace: 'pre-wrap', wordBreak: 'break-word',
              fontFamily: msg.mono ? MONO : 'inherit',
            }}
          >
            {msg.content}
          </Typography>
        ) : null}
        {msg.draft ? (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 1.25 }}>
            <Button
              size="small"
              disableElevation
              variant={msg.draftPosted ? 'text' : 'contained'}
              disabled={msg.draftPosted}
              onClick={() => onReopenDraft?.(msg.draft)}
              sx={{ minHeight: 28, px: 1.25, fontSize: 12, borderRadius: '5px',
                    ...(msg.draftPosted
                      ? { color: C.inkMute }
                      : { background: C.accent, '&:hover': { background: '#16304F' } }) }}
            >
              {msg.draftPosted ? 'Done' : 'Review →'}
            </Button>
            {!msg.draftPosted ? (
              <Typography sx={{ fontSize: 11.5, color: C.inkMute }}>
                Nothing written yet
              </Typography>
            ) : null}
          </Box>
        ) : null}
        {msg.card?.kind === 'voucher' ? <VoucherCard card={msg.card} onCommand={onCommand} /> : null}
        {msg.card?.kind === 'profile' ? <ProfileCard card={msg.card} onCommand={onCommand} /> : null}
        {msg.card?.kind === 'chart' ? <ChartCard card={msg.card} onCommand={onCommand} /> : null}
        {msg.card?.kind === 'account' ? <AccountCard card={msg.card} onCommand={onCommand} /> : null}
        {msg.card?.kind === 'voucher_list'
          ? <VoucherListCard card={msg.card} onBulkEdit={onBulkEdit} busy={busy} /> : null}
      </Paper>
    </Box>
  );
};

// -----------------------------------------------------------------------------
// Reference panel — the command grammar, not a form.
// -----------------------------------------------------------------------------
const COMMANDS = [
  {
    group: 'Record',
    items: [
      'Paid $450 to Handy Fix LLC for Repair and Maintenance from Bank of America 9523 on 06/04/2026',
      'Received $659.25 from John Smith today via Bank of America 9523 for Healthcare Services',
      'Paid $780 to Pixel Studio for EXPENSE/Website Development, cheque 4521',
      '06/10/2026 Current Assets - ACME OFFICE SUPPLY 129.40 office supplies',
    ],
  },
  {
    group: 'Review and edit',
    hint: 'The id can come first or after the verb — both work.',
    items: [
      'show 260902000001',
      '260902000001',
      'update 260902000001 amount 500',
      '260902000001 amount 500',
      '260902000001 update category Printing, date 06/10/2026',
      'update 260902000001 party Handy Fix LLC, bank Bank of America 9523',
      'void 260902000001',
    ],
  },
  {
    group: 'Profiles',
    items: [
      'add vendor Handy Fix LLC, email ops@handyfix.com, phone 555-0143',
      'new customer Acme Corp',
      'add employee Maria Lopez, phone 555-0192, title Nurse',
    ],
  },
  {
    group: 'Chart of accounts',
    items: [
      'add expense account Fuel',
      'add revenue account Consulting Income',
      'add bank account Meezan 1234',
      'add account FICA under Payroll Taxes',
    ],
  },
  {
    group: 'Look up',
    items: ['show my transactions', 'show my financial summary',
            'show chart', 'expense chart', 'revenue chart'],
  },
];

const ReferencePanel = ({ onPick }) => (
  <Box sx={{ p: 2 }}>
    <Typography sx={{ fontSize: 13, fontWeight: 600, mb: 0.5 }}>Commands</Typography>
    <Typography sx={{ fontSize: 12, color: C.inkMute, mb: 2 }}>
      Everything is typed. Click a line to load it into the composer.
    </Typography>
    {COMMANDS.map((g) => (
      <Box key={g.group} sx={{ mb: 2.25 }}>
        <Typography
          sx={{ fontSize: 11, fontWeight: 600, color: C.inkMute, letterSpacing: '0.06em', mb: 0.75 }}
        >
          {g.group.toUpperCase()}
        </Typography>
        {g.hint ? (
          <Typography sx={{ fontSize: 11.5, color: C.inkMute, mb: 0.75 }}>{g.hint}</Typography>
        ) : null}
        {g.items.map((t) => (
          <Box
            key={t}
            onClick={() => onPick(t)}
            sx={{
              px: 1, py: 0.75, mb: 0.5, borderRadius: '5px', cursor: 'pointer',
              border: `1px solid ${C.line}`, background: C.surface,
              fontFamily: MONO, fontSize: 11.5, color: C.inkMid, lineHeight: 1.5,
              '&:hover': { borderColor: C.lineStrong, background: C.raised, color: C.ink },
            }}
          >
            {t}
          </Box>
        ))}
      </Box>
    ))}
    <Divider sx={{ my: 2 }} />
    <Typography sx={{ fontSize: 11.5, color: C.inkMute, lineHeight: 1.65 }}>
      Editable fields: amount, date, party, bank, category, cheque, note. Only the fields you
      name change. Separate them with commas. Account names work either way —
      “Repair and Maintenance” or the full “EXPENSE/Repair and Maintenance”.
    </Typography>
  </Box>
);

// -----------------------------------------------------------------------------
// App
// -----------------------------------------------------------------------------
// The toolbar. Each button drops the start of a command into the composer and
// puts the cursor after it, so the button teaches the grammar rather than
// hiding it — everything here stays typeable.
const QUICK_ACTIONS = [
  { key: 'voucher', label: 'Add voucher', Icon: AddIcon, mode: 'post', prefill: '',
    hue: C.accent, soft: C.accentSoft, edge: '#D6E0EF',
    title: 'Describe a payment or receipt in one line' },
  { key: 'fix', label: 'Fix voucher', Icon: EditIcon, mode: 'update', prefill: 'update ',
    hue: C.warn, soft: C.warnSoft, edge: '#FEDF89',
    title: 'Change or void a voucher by its id' },
  { key: 'customer', label: 'Add customer', Icon: PersonAddIcon, mode: 'profile',
    prefill: 'add customer ', hue: C.blue, soft: C.blueSoft, edge: '#CBE2F4',
    title: 'Create a customer, vendor or employee profile' },
  { key: 'chart', label: 'Add chart of accounts', Icon: AccountTreeIcon, mode: 'post',
    prefill: 'add account ', hue: C.violet, soft: C.violetSoft, edge: '#DDD3F7',
    title: 'Add an account to the chart' },
];

const PLACEHOLDERS = {
  post: 'Paid $450 to Handy Fix LLC for Repair and Maintenance from Bank of America 9523',
  update: 'update 260902000001 amount 500, category Printing',
  profile: 'add vendor Handy Fix LLC, email ops@handyfix.com',
};

const GREETING = {
  id: 1,
  type: 'bot',
  timestamp: new Date(),
  content:
    'Ready. Describe a payment or receipt and I will post it to LockInLedger, ' +
    'or name a voucher by its id to review, edit or void it.\n\n' +
    '    Paid $450 to Handy Fix LLC for Repair and Maintenance from Bank of America 9523\n' +
    '    show 260902000001\n' +
    '    update 260902000001 amount 500, category Printing\n' +
    '    add vendor Handy Fix LLC, email ops@handyfix.com\n\n' +
    'Type "help" for the full list.',
};

// What each command needs, as a reference card. This is the rail's resting
// state — the panel you see when you are not reviewing a draft or looking at
// history — so it answers the question a new operator actually has: "what do
// I have to say for this to work?"
//
// Required fields are a filled dot, optional ones hollow, and every card
// carries a real example that loads straight into the composer.
const FIELD_GUIDE = [
  {
    key: 'cpv', title: 'Cash Payment Voucher', tag: 'CPV', hue: C.accent,
    blurb: 'Money out',
    fields: [
      ['Date', 'today if unsaid', false],
      ['Pay to (vendor)', '', true],
      ['Bank / cash account', '', true],
      ['Expense account', '', true],
      ['Amount', '', true],
      ['Cheque no.', 'optional', false],
      ['Remarks', 'optional', false],
    ],
    example: 'Paid $450 to Handy Fix LLC for Repair and Maintenance from Bank of America 9523',
  },
  {
    key: 'crv', title: 'Cash Receipt Voucher', tag: 'CRV', hue: '#065F46',
    blurb: 'Money in',
    fields: [
      ['Date', 'today if unsaid', false],
      ['Received from (customer)', '', true],
      ['Bank / cash account', '', true],
      ['Income account', '', true],
      ['Amount', '', true],
      ['Cheque no.', 'optional', false],
      ['Remarks', 'optional', false],
    ],
    example: 'Received $2,000 from Medicare for Healthcare Services into Meezan 1234',
  },
  {
    key: 'party', title: 'New Customer', tag: 'PROFILE', hue: C.blue,
    blurb: 'Also vendor, employee or other',
    fields: [
      ['Kind', 'customer / vendor / employee', true],
      ['Name', '', true],
      ['Email', 'optional', false],
      ['Phone', 'optional', false],
      ['Address, city, state', 'optional', false],
      ['Default account', 'optional', false],
    ],
    example: 'add vendor Handy Fix LLC, email ops@handyfix.com, phone 555-0143',
  },
  {
    key: 'chart', title: 'Chart of Accounts', tag: 'ACCOUNT', hue: C.violet,
    blurb: 'A new heading or ledger account',
    fields: [
      ['Account name', '', true],
      ['Where it goes', 'a nature or a heading', true],
      ['Kind', 'bank / expense / revenue…', false],
    ],
    example: 'add bank account Meezan 1234',
  },
];

const GuideCard = ({ card, onPick }) => (
  <Box sx={{ mb: 1.5, border: `1px solid ${C.line}`, borderRadius: '8px',
             overflow: 'hidden', background: C.surface }}>
    <Box sx={{ background: card.hue, color: '#fff', px: 1.5, py: 0.9,
               display: 'flex', alignItems: 'center', justifyContent: 'space-between',
               gap: 1 }}>
      <Box sx={{ minWidth: 0 }}>
        <Typography sx={{ fontSize: 12.5, fontWeight: 600, lineHeight: 1.3 }} noWrap>
          {card.title}
        </Typography>
        <Typography sx={{ fontSize: 10.5, opacity: 0.8 }}>{card.blurb}</Typography>
      </Box>
      <Box component="span"
           sx={{ flexShrink: 0, px: 0.6, py: '1px', borderRadius: '4px',
                 background: 'rgba(255,255,255,0.18)', fontSize: 9.5,
                 fontWeight: 700, letterSpacing: '0.06em' }}>
        {card.tag}
      </Box>
    </Box>

    <Box sx={{ px: 1.5, py: 0.9 }}>
      {card.fields.map(([label, note, required]) => (
        <Box key={label}
             sx={{ display: 'flex', alignItems: 'center', gap: 0.9, py: 0.32 }}>
          <Box sx={{ width: 5, height: 5, borderRadius: '50%', flexShrink: 0,
                     background: required ? card.hue : 'transparent',
                     border: `1px solid ${required ? card.hue : C.lineStrong}` }} />
          <Typography sx={{ fontSize: 11.5, color: C.ink, flex: 1, minWidth: 0 }} noWrap>
            {label}
          </Typography>
          {note ? (
            <Typography sx={{ fontSize: 10, color: C.inkMute, flexShrink: 0 }}>
              {note}
            </Typography>
          ) : null}
        </Box>
      ))}
    </Box>

    <Box
      onClick={() => onPick(card.example)}
      sx={{ px: 1.5, py: 0.9, borderTop: `1px solid ${C.line}`, cursor: 'pointer',
            background: C.raised, '&:hover': { background: C.surface } }}
    >
      <Typography sx={{ fontSize: 9.5, fontWeight: 700, letterSpacing: '0.06em',
                        color: C.inkMute, mb: 0.3 }}>
        EXAMPLE — CLICK TO USE
      </Typography>
      <Mono sx={{ fontSize: 10.5, color: card.hue, lineHeight: 1.5,
                  display: 'block' }}>
        {card.example}
      </Mono>
    </Box>
  </Box>
);

const FieldGuidePanel = ({ onPick }) => (
  <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
    <Box sx={{ px: 2, py: 1.5, borderBottom: `1px solid ${C.line}`, flexShrink: 0 }}>
      <Typography sx={{ fontSize: 12, fontWeight: 600, color: C.ink }}>
        Form fields guide
      </Typography>
      <Typography sx={{ fontSize: 11.5, color: C.inkMute }}>
        What each entry needs
      </Typography>
    </Box>

    <Box sx={{ flex: 1, overflowY: 'auto', p: 1.5 }}>
      {FIELD_GUIDE.map((c) => <GuideCard key={c.key} card={c} onPick={onPick} />)}

      <Box sx={{ p: 1.5, borderRadius: '8px', background: C.accentSoft,
                 border: `1px solid #D6E0EF` }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mb: 0.6 }}>
          <LightbulbOutlinedIcon sx={{ fontSize: 15, color: C.accent }} />
          <Typography sx={{ fontSize: 11.5, fontWeight: 700, color: C.accent }}>
            How it works
          </Typography>
        </Box>
        <Typography sx={{ fontSize: 11, color: C.inkMid, lineHeight: 1.65 }}>
          Say it in one sentence — order does not matter, and anything you leave
          out is either defaulted or asked for. Press Enter and the entry comes
          back here as a draft you can correct. <b>Nothing reaches the ledger
          until you press Post.</b>
        </Typography>
        <Box sx={{ mt: 0.9, pt: 0.9, borderTop: `1px solid #D6E0EF` }}>
          <Typography sx={{ fontSize: 11, color: C.inkMid, lineHeight: 1.65 }}>
            A filled dot is required, a hollow one optional. Account names work
            either way — “Repair and Maintenance” or the full
            “EXPENSE/Repair and Maintenance”.
          </Typography>
        </Box>
      </Box>
    </Box>
  </Box>
);

// Session history — the rail that used to always be there, now opened from
// the toolbar.
const HistoryPanel = ({ activity, onPick, onClose }) => (
  <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
    <Box sx={{ px: 2, py: 1.5, borderBottom: `1px solid ${C.line}`, display: 'flex',
               alignItems: 'center', justifyContent: 'space-between', gap: 1 }}>
      <Box sx={{ minWidth: 0 }}>
        <Typography sx={{ fontSize: 12, fontWeight: 600, color: C.ink }}>This session</Typography>
        <Typography sx={{ fontSize: 11.5, color: C.inkMute }}>
          {activity.length} voucher{activity.length === 1 ? '' : 's'} touched
        </Typography>
      </Box>
      {onClose ? (
        <IconButton size="small" onClick={onClose} sx={{ color: C.inkMid }}>
          <CloseIcon sx={{ fontSize: 17 }} />
        </IconButton>
      ) : null}
    </Box>
    <Box sx={{ flex: 1, overflowY: 'auto', p: 1.25 }}>
      {activity.length === 0 ? (
        <Typography sx={{ fontSize: 12, color: C.inkMute, px: 0.75, py: 1 }}>
          Vouchers you post or edit appear here.
        </Typography>
      ) : (
        activity.map((a) => (
          <Box
            key={a.key}
            onClick={() => onPick(`show ${a.atId || a.voucherNo}`)}
            sx={{
              px: 1.25, py: 1, mb: 0.5, borderRadius: '6px', cursor: 'pointer',
              border: `1px solid ${C.line}`,
              '&:hover': { borderColor: C.lineStrong, background: C.raised },
            }}
          >
            <Box sx={{ display: 'flex', alignItems: 'center',
                       justifyContent: 'space-between', gap: 1 }}>
              <Mono sx={{ fontSize: 11.5, color: C.inkMid }}>{a.voucherNo}</Mono>
              <Pill label={a.entryType} tone="accent" />
            </Box>
            <Typography sx={{ fontFamily: MONO, fontSize: 13, fontWeight: 600,
                              color: C.ink, mt: 0.4 }}>
              ${money(a.amount)}
            </Typography>
            <Typography sx={{ fontSize: 11.5, color: C.inkMute }} noWrap>
              {a.party || '—'} · {a.verb} {clockTime(a.at)}
            </Typography>
          </Box>
        ))
      )}
    </Box>
  </Box>
);

export default function App() {
  const [messages, setMessages] = useState([]);
  const [inputMessage, setInputMessage] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [sessionId, setSessionId] = useState('');
  const [mode, setMode] = useState('post');
  const [connection, setConnection] = useState({ state: 'checking', detail: '' });
  const [accountCount, setAccountCount] = useState(null);
  const [refOpen, setRefOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [activity, setActivity] = useState([]);
  const [snack, setSnack] = useState(null);
  // The confirmation step. `draft` is the editable, still-unwritten voucher;
  // it exists only between pressing Enter and pressing Post.
  const [draft, setDraft] = useState(null);
  const [draftMsgId, setDraftMsgId] = useState(null);
  const [posting, setPosting] = useState(false);
  // A bulk edit is a queue of drafts stepped through one at a time; a single
  // edit is just a queue of one.
  const [queue, setQueue] = useState(null);   // { drafts, index, saved:Set }
  const [postError, setPostError] = useState(null);
  // The real chart and party list, so a correction picks a code that exists.
  const [pickers, setPickers] = useState({ bank: [], income: [], expense: [], all: [] });
  const [parties, setParties] = useState([]);

  const endRef = useRef(null);
  const inputRef = useRef(null);

  // One axios instance for the app's lifetime — rebuilding it each render
  // re-registers interceptors and leaks them.
  const api = useMemo(
    () => axios.create({ baseURL: API_BASE_URL, timeout: 60000, headers: { 'Content-Type': 'application/json' } }),
    []
  );

  const showSnack = useCallback((message, severity = 'info') => setSnack({ message, severity }), []);

  const checkConnection = useCallback(async () => {
    setConnection({ state: 'checking', detail: '' });
    try {
      const r = await api.get('/api/health');
      const ok = r.data?.database === 'connected' || r.data?.status === 'ok' || r.data?.status === 'healthy';
      setConnection({ state: ok ? 'online' : 'degraded', detail: r.data?.database || '' });
      // The chart and the party list back the pickers in the review panel, so
      // a correction is always chosen from what actually exists.
      try {
        const a = await api.get('/api/accounts');
        const d = a.data || {};
        const all = d.all || d.accounts || (Array.isArray(a.data) ? a.data : []);
        setPickers({ bank: d.bank || all, income: d.income || [], expense: d.expense || [], all });
        setAccountCount(all.length || null);
      } catch {
        setAccountCount(null);
      }
      try {
        const p = await api.get('/api/parties');
        setParties(p.data?.parties || []);
      } catch {
        setParties([]);
      }
    } catch (e) {
      setConnection({ state: 'offline', detail: e?.message || 'unreachable' });
    }
  }, [api]);

  useEffect(() => {
    setSessionId('session_' + Date.now() + '_' + Math.random().toString(36).slice(2, 11));
    setMessages([{ ...GREETING, timestamp: new Date() }]);
    checkConnection();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isLoading]);

  const loadIntoComposer = useCallback((text, focusOnly) => {
    setInputMessage(text);
    setRefOpen(false);
    const el = inputRef.current;
    if (el) {
      el.focus();
      if (focusOnly) requestAnimationFrame(() => el.setSelectionRange(text.length, text.length));
    }
  }, []);

  // Shared by a direct post and by a confirmed draft, so a voucher looks the
  // same in the transcript however it got there.
  const recordVoucherResult = useCallback((data, { verb = 'posted' } = {}) => {
    const voucherNo = data.card?.voucher_number || data.voucher_number;
    const atId = data.card?.at_id || data.at_id;
    if (!voucherNo) return;
    setActivity((prev) => [
      {
        key: `${voucherNo}-${Date.now()}`,
        atId,
        voucherNo,
        entryType: data.card?.entry_type || data.entry_type,
        amount: data.card?.amount ?? data.amount,
        party: data.card?.party_name || data.party_name,
        verb,
        at: new Date(),
      },
      ...prev.filter((a) => a.voucherNo !== voucherNo),
    ]);
    showSnack(`${voucherNo} ${verb}`, verb === 'voided' ? 'warning' : 'success');
  }, [showSnack]);

  // A toolbar button sets the mode hint and seeds the composer; the operator
  // finishes the sentence.
  const runQuickAction = useCallback((a) => {
    setMode(a.mode);
    setInputMessage(a.prefill);
    const el = inputRef.current;
    if (el) {
      el.focus();
      requestAnimationFrame(() =>
        el.setSelectionRange(a.prefill.length, a.prefill.length));
    }
  }, []);

  // Clears the transcript only — vouchers already posted are in the ledger,
  // and the session history keeps its record of them.
  const clearChat = useCallback(() => {
    setMessages([{ ...GREETING, timestamp: new Date() }]);
    setInputMessage('');
    setDraft(null);
    setDraftMsgId(null);
    setPostError(null);
    inputRef.current?.focus();
  }, []);

  const sendMessage = async (overrideText) => {
    const text = (overrideText ?? inputMessage).trim();
    if (!text || isLoading) return;

    setMessages((prev) => [...prev, { id: Date.now(), type: 'user', content: text, timestamp: new Date() }]);
    setInputMessage('');
    setIsLoading(true);

    try {
      // preview:true means a message that would CREATE a voucher comes back as
      // a draft instead of a posting. Everything else (show / update / void /
      // profile / chart) still executes, so this is the only call needed.
      const { data } = await api.post('/api/chat', {
        session_id: sessionId, message: text, mode, preview: true,
      });

      // ---- the confirmation step -----------------------------------------
      if (data.status === 'draft' && data.draft) {
        const id = Date.now() + 1;
        const d = { ...data.draft, review_items: data.review_items || [] };
        setMessages((prev) => [
          ...prev,
          {
            id,
            type: 'bot',
            timestamp: new Date(),
            content: data.review_items?.length
              ? `Ready — check the ${data.review_items.join(', ')} on the right.`
              : 'Ready — review the details on the right.',
            draft: d,
          },
        ]);
        setDraft(d);
        setDraftMsgId(id);
        setPostError(null);
        return;
      }

      const isFreshVoucher = !!data.voucher_number && !data.card;
      const content = isFreshVoucher ? '' : data.message || data.analysis || 'Done.';

      setMessages((prev) => [
        ...prev,
        {
          id: Date.now() + 1,
          type: 'bot',
          timestamp: new Date(),
          content,
          // A fresh post arrives as flat fields; VoucherCard normalises either
          // shape, so it renders the same as a looked-up voucher.
          card: data.card || (isFreshVoucher ? { kind: 'voucher', ...data } : null),
          // A business rejection (ambiguous account, locked voucher, unknown
          // party) is an error even though the HTTP call succeeded.
          isError: data.status === 'error',
        },
      ]);

      // `action` is set by the backend for the commands that change something.
      // A plain `show` must not be logged as activity or announced as a write.
      const wrote = isFreshVoucher ? 'posted' : { updated: 'updated', voided: 'voided' }[data.action];

      if (wrote && data.status !== 'error') {
        recordVoucherResult(data, { verb: wrote });
      } else if (data.action === 'profile' && data.status !== 'error') {
        showSnack(
          `${data.card?.company_name || 'Profile'} ${data.card?.created ? 'created' : 'already existed'}`,
          'success'
        );
      } else if (data.status === 'error') {
        showSnack('Nothing was written — see the reply', 'warning');
      }
    } catch (error) {
      const detail = error?.response?.data?.detail || error?.message || 'unknown error';
      setMessages((prev) => [
        ...prev,
        {
          id: Date.now() + 1,
          type: 'bot',
          isError: true,
          timestamp: new Date(),
          content: `Could not reach the ledger service at ${API_BASE_URL}.\n${detail}`,
        },
      ]);
      showSnack('Request failed', 'error');
    } finally {
      setIsLoading(false);
    }
  };

  // The reviewed draft goes back by CODE, so what is written is what was on
  // screen — the parser does not get a second turn.
  // Each kind of draft has its own commit endpoint; all three take the
  // reviewed values (codes, not names) and return the same reply shape.
  const COMMIT_ENDPOINT = { voucher: '/api/commit', party: '/api/commit/party',
                            account: '/api/commit/account', edit: '/api/commit/edit' };

  const commitBody = (d, sessionId) => {
    const kind = d.kind || 'voucher';
    if (kind === 'party') {
      return {
        session_id: sessionId, p_type: d.p_type,
        company_name: d.company_name || null, person_name: d.person_name || null,
        email: d.email || null, phone: d.phone || null, fax: d.fax || null,
        address: d.address || null, city: d.city || null, state: d.state || null,
        zipcode: d.zipcode || null, job_title: d.job_title || null,
        sale_tax_no: d.sale_tax_no || null, fedral_id_no: d.fedral_id_no || null,
        business_desc: d.business_desc || null, other_desc: d.other_desc || null,
        p_account: d.p_account || null, source_message: d.source_message || null,
      };
    }
    if (kind === 'account') {
      return {
        session_id: sessionId, name: d.name, level: d.level,
        parent_code: d.parent_code, source_message: d.source_message || null,
      };
    }
    if (kind === 'edit') {
      return {
        session_id: sessionId, at_id: d.at_id,
        amount: Number(d.amount),
        transaction_date: d.transaction_date,
        party_code: d.party_code || null,
        bank_acc_code: d.bank_acc_code,
        category_acc_code: d.category_acc_code,
        cheque_no: d.cheque_no ?? '',
        description: d.description ?? '',
      };
    }
    return {
      session_id: sessionId,
      entry_type: d.entry_type,
      amount: Number(d.amount),
      transaction_date: d.transaction_date,
      party_code: d.party_code || null,
      party_name: d.party_name || null,
      bank_acc_code: d.bank_acc_code,
      category_acc_code: d.category_acc_code,
      cheque_no: d.cheque_no || null,
      description: d.description || null,
      source_message: d.source_message || null,
    };
  };

  const postDraft = async (d) => {
    if (posting) return;
    const kind = d.kind || 'voucher';
    setPosting(true);
    setPostError(null);
    try {
      const { data } = await api.post(COMMIT_ENDPOINT[kind] || '/api/commit',
                                      commitBody(d, sessionId));

      if (data.status === 'error') {
        setPostError(data.message || 'The ledger refused this entry.');
        showSnack('Nothing was written — see the panel', 'warning');
        return;
      }

      // A voucher rebuilds its card from the flat payload; the other two
      // already come back with the card their chat command would have made.
      setMessages((prev) => [
        ...prev.map((m) => (m.id === draftMsgId ? { ...m, draftPosted: true } : m)),
        {
          id: Date.now() + 1,
          type: 'bot',
          timestamp: new Date(),
          content: kind === 'voucher' ? '' : (data.message || data.analysis || 'Done.'),
          card: kind === 'voucher' ? { kind: 'voucher', ...data } : data.card || null,
        },
      ]);

      if (kind === 'voucher') {
        recordVoucherResult(data, { verb: 'posted' });
        // A brand new party is now a real profile — keep the picker current.
        if (!d.party_code && data.party_code) {
          setParties((prev) => [
            ...prev,
            { p_code: data.party_code, p_type: data.party_type,
              company_name: data.party_name, person_name: data.party_name },
          ]);
        }
      } else if (kind === 'party') {
        showSnack(`${data.card?.company_name || 'Profile'} ` +
                  `${data.card?.created ? 'created' : 'already existed'}`, 'success');
        if (data.card?.p_code) {
          setParties((prev) => [
            ...prev.filter((p) => String(p.p_code) !== String(data.card.p_code)),
            { p_code: data.card.p_code, p_type: data.card.p_type,
              company_name: data.card.company_name,
              person_name: data.card.person_name },
          ]);
        }
      } else if (kind === 'edit') {
        recordVoucherResult(data, { verb: 'updated' });
      } else {
        showSnack(`${data.card?.name || 'Account'} added to the chart`, 'success');
        // The chart grew — refresh the pickers so it can be used at once.
        checkConnection();
      }

      // In a queue, saving moves to the next voucher rather than closing the
      // panel — that is the whole point of stepping through a bulk edit.
      if (kind === 'edit' && queue && queue.drafts.length > 1) {
        const saved = new Set(queue.saved).add(d.at_id);
        const nextIdx = queue.drafts.findIndex((x, i) => i > queue.index && !saved.has(x.at_id));
        const fallback = queue.drafts.findIndex((x) => !saved.has(x.at_id));
        const go = nextIdx >= 0 ? nextIdx : fallback;
        if (go >= 0) {
          setQueue({ ...queue, index: go, saved });
          setDraft(queue.drafts[go]);
          setPosting(false);
          return;
        }
        setQueue(null);
        showSnack(`All ${saved.size} vouchers saved`, 'success');
      }

      setDraft(null);
      setDraftMsgId(null);
      setQueue(null);
    } catch (error) {
      const detail = error?.response?.data?.detail || error?.message || 'unknown error';
      setPostError(`Could not reach the ledger service.\n${detail}`);
      showSnack('Request failed', 'error');
    } finally {
      setPosting(false);
    }
  };

  const discardDraft = () => {
    setDraft(null);
    setDraftMsgId(null);
    setQueue(null);
    setPostError(null);
    inputRef.current?.focus();
  };

  // Ticked vouchers, opened as drafts. The optional instruction is resolved
  // server-side onto every one of them — and still shown for confirmation.
  const bulkEdit = useCallback(async (atIds, instruction) => {
    if (!atIds?.length || posting) return;
    setPosting(true);
    setPostError(null);
    try {
      const { data } = await api.post('/api/edit/batch', {
        session_id: sessionId, at_ids: atIds, instruction: instruction || null,
      });
      if (data.status === 'error' || !data.drafts?.length) {
        showSnack(data.message || 'Nothing there can be edited', 'warning');
        setMessages((prev) => [...prev, {
          id: Date.now(), type: 'bot', timestamp: new Date(), isError: true,
          content: data.message || 'Nothing there can be edited.',
        }]);
        return;
      }
      const skipped = (data.errors || []).length;
      setMessages((prev) => [...prev, {
        id: Date.now(), type: 'bot', timestamp: new Date(),
        content: `${data.drafts.length} voucher${data.drafts.length === 1 ? '' : 's'} `
               + `open for editing${skipped ? ` — ${skipped} skipped `
               + `(${(data.errors || []).map((e) => e.voucher_number || e.at_id).join(', ')})` : ''}.`
               + `\nStep through them on the right; each one saves on its own.`,
      }]);
      setQueue({ drafts: data.drafts, index: 0, saved: new Set() });
      setDraft(data.drafts[0]);
      setDraftMsgId(null);
    } catch (error) {
      showSnack('Could not open those vouchers', 'error');
    } finally {
      setPosting(false);
    }
  }, [api, sessionId, posting, showSnack]);

  // Move within the queue, keeping any edits made to the draft being left.
  const goToDraft = (idx) => {
    if (!queue || idx < 0 || idx >= queue.drafts.length) return;
    const drafts = queue.drafts.slice();
    if (draft) drafts[queue.index] = draft;
    setQueue({ ...queue, drafts, index: idx });
    setDraft(drafts[idx]);
    setPostError(null);
  };

  const reopenDraft = useCallback((d) => {
    setDraft(d);
    setPostError(null);
  }, []);

  const draftPanel = draft ? (
    <DraftPanel
      draft={draft}
      setDraft={setDraft}
      pickers={pickers}
      parties={parties}
      onPost={postDraft}
      onDiscard={discardDraft}
      posting={posting}
      error={postError}
      queue={queue ? {
        index: queue.index,
        total: queue.drafts.length,
        savedCount: queue.saved.size,
        onPrev: () => goToDraft(queue.index - 1),
        onNext: () => goToDraft(queue.index + 1),
      } : null}
    />
  ) : null;

  const dot =
    connection.state === 'online' ? C.ok : connection.state === 'checking' ? C.inkMute : C.err;

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <Box sx={{ height: '100vh', display: 'flex', flexDirection: 'column', background: C.bg }}>
        {/* Header ------------------------------------------------------- */}
        <Box
          component="header"
          sx={{
            height: 52, flexShrink: 0, px: 2.5, display: 'flex', alignItems: 'center',
            gap: 2, background: C.surface, borderBottom: `1px solid ${C.line}`,
          }}
        >
          <Typography sx={{ fontSize: 14, fontWeight: 600, letterSpacing: '-0.01em' }}>
            LockInLedger
          </Typography>
          <Typography sx={{ fontSize: 14, color: C.inkMute }}>Assistant</Typography>

          <Box sx={{ flex: 1 }} />

          <Tooltip title={connection.detail || connection.state}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
              <Box sx={{ width: 6, height: 6, borderRadius: '50%', background: dot }} />
              <Typography sx={{ fontSize: 12, color: C.inkMid, textTransform: 'capitalize' }}>
                {connection.state}
              </Typography>
            </Box>
          </Tooltip>

          {accountCount != null ? (
            <Typography sx={{ fontSize: 12, color: C.inkMute, display: { xs: 'none', sm: 'block' } }}>
              {accountCount} accounts
            </Typography>
          ) : null}

          <Tooltip title="Recheck connection">
            <IconButton size="small" onClick={checkConnection} sx={{ color: C.inkMid }}>
              <RefreshIcon sx={{ fontSize: 16 }} />
            </IconButton>
          </Tooltip>

          <Button
            size="small"
            onClick={() => setRefOpen(true)}
            sx={{
              fontSize: 12, color: C.inkMid, border: `1px solid ${C.line}`,
              borderRadius: '5px', minHeight: 28, px: 1.25,
              '&:hover': { borderColor: C.lineStrong, background: C.raised },
            }}
          >
            Commands
          </Button>
        </Box>

        {/* Toolbar ------------------------------------------------------ */}
        <Box
          sx={{
            flexShrink: 0, px: { xs: 1.5, md: 2.5 }, py: 1, display: 'flex',
            alignItems: 'center', gap: 1, background: C.surface,
            borderBottom: `1px solid ${C.line}`,
          }}
        >
          <Box
            sx={{
              display: 'flex', alignItems: 'center', gap: 1, flex: 1, minWidth: 0,
              overflowX: 'auto', '&::-webkit-scrollbar': { height: 0 },
              scrollbarWidth: 'none',
            }}
          >
          <Typography
            sx={{ fontSize: 11.5, fontWeight: 600, color: C.inkMute, flexShrink: 0,
                  letterSpacing: '0.02em', display: { xs: 'none', sm: 'block' }, mr: 0.25 }}
          >
            Quick create
          </Typography>

          {QUICK_ACTIONS.map((a) => (
            <Tooltip key={a.key} title={a.title}>
              <Button
                size="small"
                onClick={() => runQuickAction(a)}
                startIcon={<a.Icon sx={{ fontSize: 15 }} />}
                sx={{
                  flexShrink: 0, minHeight: 30, px: 1.25, fontSize: 12.5, fontWeight: 600,
                  color: a.hue, background: a.soft, border: `1px solid ${a.edge}`,
                  borderRadius: '6px', whiteSpace: 'nowrap',
                  '& .MuiButton-startIcon': { mr: 0.6 },
                  '&:hover': { background: a.soft, borderColor: a.hue },
                }}
              >
                {a.label}
              </Button>
            </Tooltip>
          ))}

          </Box>

          <Button
            size="small"
            onClick={() => setHistoryOpen((o) => !o)}
            startIcon={<HistoryIcon sx={{ fontSize: 15 }} />}
            sx={{
              flexShrink: 0, minHeight: 30, px: 1.25, fontSize: 12.5, borderRadius: '6px',
              whiteSpace: 'nowrap',
              color: historyOpen ? C.accent : C.inkMid,
              background: historyOpen ? C.accentSoft : 'transparent',
              border: `1px solid ${historyOpen ? '#D6E0EF' : C.line}`,
              '& .MuiButton-startIcon': { mr: 0.6 },
              '&:hover': { borderColor: C.lineStrong,
                           background: historyOpen ? C.accentSoft : C.raised },
            }}
          >
            History{activity.length ? ` (${activity.length})` : ''}
          </Button>

          <Tooltip title="Clear the conversation — posted vouchers are unaffected">
            <Button
              size="small"
              onClick={clearChat}
              startIcon={<DeleteOutlineIcon sx={{ fontSize: 15 }} />}
              sx={{
                flexShrink: 0, minHeight: 30, px: 1.25, fontSize: 12.5, color: C.inkMid,
                border: `1px solid ${C.line}`, borderRadius: '6px', whiteSpace: 'nowrap',
                '& .MuiButton-startIcon': { mr: 0.6 },
                '&:hover': { borderColor: C.lineStrong, background: C.raised },
              }}
            >
              Clear
            </Button>
          </Tooltip>
        </Box>

        {/* Body --------------------------------------------------------- */}
        <Box sx={{ flex: 1, minHeight: 0, display: 'grid',
                   gridTemplateColumns: { xs: '1fr',
                     lg: draft ? '1fr 380px' : '1fr 320px' } }}>
          {/* Conversation */}
          <Box sx={{ display: 'flex', flexDirection: 'column', minWidth: 0, minHeight: 0 }}>
            <Box sx={{ flex: 1, overflowY: 'auto', px: { xs: 2, md: 4 }, py: 3 }}>
              <Box sx={{ maxWidth: 780, mx: 'auto' }}>
                {messages.map((m) => (
                  <Message key={m.id} msg={m} onCommand={loadIntoComposer}
                           onReopenDraft={reopenDraft} onBulkEdit={bulkEdit}
                           busy={posting} />
                ))}
                {isLoading ? (
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.25, mb: 2.5, color: C.inkMute }}>
                    <CircularProgress size={12} thickness={5} sx={{ color: C.inkMute }} />
                    <Typography sx={{ fontSize: 12.5 }}>Working…</Typography>
                  </Box>
                ) : null}
                <div ref={endRef} />
              </Box>
            </Box>

            {/* Composer */}
            <Box
              sx={{
                flexShrink: 0, borderTop: `1px solid ${C.line}`, background: C.surface,
                px: { xs: 2, md: 4 }, py: 1.75,
              }}
            >
              <Box sx={{ maxWidth: 780, mx: 'auto' }}>
                <Box sx={{ display: 'flex', alignItems: 'flex-end', gap: 1 }}>
                  <TextField
                    inputRef={inputRef}
                    fullWidth
                    multiline
                    maxRows={8}
                    value={inputMessage}
                    onChange={(e) => setInputMessage(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        sendMessage();
                      }
                    }}
                    placeholder={PLACEHOLDERS[mode] || PLACEHOLDERS.post}
                    disabled={isLoading}
                    sx={{
                      '& .MuiOutlinedInput-root': {
                        fontSize: 13.5, background: C.raised, borderRadius: '7px',
                        '& fieldset': { borderColor: C.line },
                        '&:hover fieldset': { borderColor: C.lineStrong },
                        '&.Mui-focused fieldset': { borderColor: C.accent, borderWidth: 1 },
                      },
                      '& .MuiOutlinedInput-input::placeholder': { color: C.inkMute, opacity: 1 },
                    }}
                  />
                  <IconButton
                    onClick={() => sendMessage()}
                    disabled={!inputMessage.trim() || isLoading}
                    sx={{
                      width: 36, height: 36, borderRadius: '7px', background: C.accent, color: '#fff',
                      '&:hover': { background: '#16304F' },
                      '&.Mui-disabled': { background: '#E8EBF0', color: C.inkMute },
                    }}
                  >
                    <ArrowUpwardIcon sx={{ fontSize: 17 }} />
                  </IconButton>
                </Box>
                <Typography sx={{ mt: 0.75, fontSize: 11, color: C.inkMute }}>
                  Enter to review · Shift+Enter for a new line · nothing posts to system 146 until you confirm
                </Typography>
              </Box>
            </Box>
          </Box>

          {/* Right rail. A pending draft owns it; History takes it when asked
              for; otherwise it rests on the field guide. */}
          <Box
            sx={{
              display: { xs: 'none', lg: 'flex' }, flexDirection: 'column',
              borderLeft: `1px solid ${C.line}`, background: C.surface, minHeight: 0,
            }}
          >
            {draft ? draftPanel
              : historyOpen ? (
                <HistoryPanel
                  activity={activity}
                  onPick={loadIntoComposer}
                  onClose={() => setHistoryOpen(false)}
                />
              ) : (
                <FieldGuidePanel onPick={(t) => loadIntoComposer(t, true)} />
              )}
          </Box>
        </Box>
      </Box>

      {/* Below lg there is no rail, so history becomes a sheet too. */}
      <Drawer
        anchor="right"
        open={historyOpen && !draft}
        onClose={() => setHistoryOpen(false)}
        sx={{ display: { xs: 'block', lg: 'none' } }}
        PaperProps={{ sx: { width: { xs: '100%', sm: 320 }, background: C.surface } }}
      >
        <HistoryPanel
          activity={activity}
          onPick={(t) => { loadIntoComposer(t); setHistoryOpen(false); }}
          onClose={() => setHistoryOpen(false)}
        />
      </Drawer>

      {/* Below lg there is no rail, so the review step becomes a sheet. */}
      <Drawer
        anchor="right"
        open={!!draft}
        onClose={discardDraft}
        sx={{ display: { xs: 'block', lg: 'none' } }}
        PaperProps={{ sx: { width: { xs: '100%', sm: 380 }, background: C.surface } }}
      >
        {draftPanel}
      </Drawer>

      {/* Commands drawer ------------------------------------------------ */}
      <Drawer
        anchor="right"
        open={refOpen}
        onClose={() => setRefOpen(false)}
        PaperProps={{ sx: { width: 380, background: C.bg } }}
      >
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', px: 2, py: 1.5, borderBottom: `1px solid ${C.line}`, background: C.surface }}>
          <Typography sx={{ fontSize: 13, fontWeight: 600 }}>Reference</Typography>
          <IconButton size="small" onClick={() => setRefOpen(false)} sx={{ color: C.inkMid }}>
            <CloseIcon sx={{ fontSize: 17 }} />
          </IconButton>
        </Box>
        <Box sx={{ overflowY: 'auto' }}>
          <ReferencePanel onPick={(t) => loadIntoComposer(t, true)} />
        </Box>
      </Drawer>

      <Snackbar
        open={!!snack}
        autoHideDuration={4000}
        onClose={() => setSnack(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert
          severity={snack?.severity || 'info'}
          variant="outlined"
          onClose={() => setSnack(null)}
          sx={{ fontSize: 12.5, background: C.surface, borderRadius: '6px' }}
        >
          {snack?.message}
        </Alert>
      </Snackbar>
    </ThemeProvider>
  );
}