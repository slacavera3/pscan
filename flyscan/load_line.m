function [ac, meta] = load_line(scan_dir, line_index, role, rep_rate_hz)
%LOAD_LINE  Read one flyscan line using the SHAPE THE FILE DECLARES.
%
%   [ac, meta] = load_line('map.flyscan', 0)
%   [ac, meta] = load_line('map.flyscan', 0, 'dc')
%   [ac, meta] = load_line('map.flyscan', 0, 'ac', 100e6)   % override rep rate
%
% Returns ac as [n_segments x points_per_segment], scaled to volts, plus a
% meta struct carrying the timing needed for a delay axis.
%
% Never hardcode points-per-segment. The scope does not always return the
% number that was asked for - a WS4024HD asked for 500 returned 502 - and
% reading an N-point file as M-point segments shears it: a feature walks by
% (N-M)*M/N samples per row, dead straight, looking exactly like an
% instrument drift. At N=502, M=500 that is +1.99203 samples/row, which
% cost a long investigation. manifest.json records the real shape; use it.
%
% The fourth argument overrides the recorded laser repetition rate, which is
% worth passing for files written before 'rep_rate' became a config key:
% those all claim 80 MHz regardless of the rig they ran on.

if nargin < 3 || isempty(role), role = 'ac'; end
if nargin < 4, rep_rate_hz = []; end

manifest = jsondecode(fileread(fullfile(scan_dir, 'manifest.json')));
line = index_into(manifest.lines, line_index + 1);

% Find the channel with the role we want.
ch = [];
for k = 1:count_of(line.channels)
    candidate = index_into(line.channels, k);
    if strcmp(candidate.role, role)
        ch = candidate; break
    end
end
if isempty(ch)
    error('load_line:noRole', 'line %d has no ''%s'' channel', ...
          line_index, role);
end

shape = double(ch.shape(:)');        % [n_segments, points_per_segment]

% The writer forces little-endian, so say so rather than trusting the
% platform default, and translate the numpy dtype string ('<i2') into a
% MATLAB precision ('int16') - they are not the same vocabulary.
fi = fopen(fullfile(scan_dir, ch.file), 'r', 'ieee-le');
if fi < 0
    error('load_line:noFile', 'cannot open %s', ...
          fullfile(scan_dir, ch.file));
end
cleanup = onCleanup(@() fclose(fi));
raw = fread(fi, Inf, [numpy_precision(ch.dtype) '=>double']);

expected = prod(shape);
if numel(raw) ~= expected
    error('load_line:sizeMismatch', ...
          ['%s holds %d samples but the manifest declares %s = %d. Not ' ...
           'reshaping - the result would be sheared.'], ...
          ch.file, numel(raw), mat2str(shape), expected);
end

% Written C-order (segment-major); MATLAB fills column-major, so read into
% the transposed shape and flip.
ac = reshape(raw, fliplr(shape))';
ac = ac * ch.v_gain + ch.v_offset;

%% timing
a = line.attrs;
meta = struct();
meta.shape              = shape;
meta.n_segments         = shape(1);
meta.points_per_segment = shape(2);
meta.sample_rate_hz     = a.sample_rate_hz;
meta.sparsing           = double(ch.sparsing);

% 'sweep_rate_hz' is what files written before the rename carry.
if isfield(a, 'delay_rate_hz')
    meta.delay_rate_hz = a.delay_rate_hz;
elseif isfield(a, 'sweep_rate_hz')
    meta.delay_rate_hz = a.sweep_rate_hz;
else
    error('load_line:noDelayRate', 'manifest has no delay rate');
end

if ~isempty(rep_rate_hz)
    meta.rep_rate_hz = rep_rate_hz;
elseif isfield(a, 'rep_rate_hz')
    meta.rep_rate_hz = a.rep_rate_hz;
else
    error('load_line:noRepRate', 'manifest has no rep rate');
end

% Recompute rather than trusting a stored stretch_factor: it was written
% from whatever rep rate the file claims, which may be the 80 MHz default.
meta.stretch_factor = meta.rep_rate_hz / meta.delay_rate_hz;

dt_lab = meta.sparsing / meta.sample_rate_hz;
meta.dt_delay_s = dt_lab / meta.stretch_factor;
meta.delay_s    = (0:meta.points_per_segment-1) * meta.dt_delay_s;

fprintf(['%s line %d %s: %d segments x %d points, %.4g ps/sample, ' ...
         '%.4g ns span\n'], scan_dir, line_index, role, ...
        meta.n_segments, meta.points_per_segment, ...
        meta.dt_delay_s * 1e12, meta.delay_s(end) * 1e9);
end

% -------------------------------------------------------------------------

function p = numpy_precision(dtype)
%NUMPY_PRECISION  '<i2' -> 'int16'. jsondecode hands back numpy's spelling.
key = strtrim(dtype);
if ~isempty(key) && any(key(1) == '<>=|')
    key = key(2:end);                 % drop the byte-order mark
end
switch lower(key)
    case {'i1', 'b'},  p = 'int8';
    case 'i2',         p = 'int16';
    case 'i4',         p = 'int32';
    case 'i8',         p = 'int64';
    case {'u1', 'b1'}, p = 'uint8';
    case 'u2',         p = 'uint16';
    case 'u4',         p = 'uint32';
    case 'f4',         p = 'single';
    case 'f8',         p = 'double';
    otherwise
        error('load_line:dtype', ...
              'unrecognised dtype ''%s'' in manifest', dtype);
end
end

function n = count_of(x)
%COUNT_OF  jsondecode gives a struct array when records match, a cell when
%they do not. Handle both rather than assuming.
if iscell(x), n = numel(x); else, n = numel(x); end
end

function item = index_into(x, k)
if iscell(x), item = x{k}; else, item = x(k); end
end
