%% measure_walk.m - how fast does the copeak walk, and what is the true beat?
%
% The copeak walks through the segment window when the ASOPS beat frequency
% is not an exact multiple of the scope trigger frequency. The walk rate
% measures that difference directly, and it is the only handle on the beat
% you have without putting a counter on the lasers.
%
% Do NOT read the walk rate off an imagesc. 10000 segments drawn into ~800
% screen pixels aliases badly and the apparent band spacing can be out by an
% integer factor. Fit it.
%
% Expects `data.ac{1}` from the loader, shaped [ypts, xpts, no_points].

sample_rate  = 10e6;       % scope, Hz
trigger_rate = 10e3;       % signal generator, Hz

ac = squeeze(data.ac{1}(1, :, :));      % [n_segments x npts]
[n_segments, npts] = size(ac);
dt_lab = 1 / sample_rate;
T_trig = 1 / trigger_rate;

%% locate the copeak in each segment
% The copeak is the sharp negative excursion. Test each segment against its
% OWN noise rather than against the loudest segment in the line - a global
% threshold passes everything when the copeak is always present, and then
% silently fits noise.
base  = median(ac, 2);
[dip, idx] = min(ac, [], 2);
depth = base - dip;
noise = 1.4826 * median(abs(ac - base), 2);
present = depth > 5 * noise;

frac = nnz(present) / n_segments;
fprintf('copeak present in %d of %d segments (%.1f%%)\n', ...
        nnz(present), n_segments, 100 * frac);

%% fit the walk by unwrapping the sawtooth
% The index wraps when the copeak leaves one end of the captured window and
% the next one enters the other. Unwrap first, then one straight line fits
% the whole line at once - far more precise than fitting a single run.
if ~all(present)
    warning(['%d segments have no copeak. Those are lost pixels, not ' ...
             'misaligned ones. Fitting the rest.'], nnz(~present));
end
s = find(present);
u = unwrap(idx(s) * 2 * pi / npts) * npts / (2 * pi);

p     = polyfit(s, u, 1);
walk  = p(1);                           % samples per segment
resid = u - polyval(p, s);

%% what the sawtooth span tells you about the beat
% Wrap modulus = min(copeak recurrence, captured window), in samples. If the
% ramps span the full window AND the copeak is present in every segment,
% the recurrence equals the window duration - which pins the beat.
span = max(idx(s)) - min(idx(s));
fprintf('sawtooth spans %d of %d samples\n', span, npts);

% Advance per segment is (-T_trig) mod T_beat, so for T_beat ~ T_trig/m:
%     T_beat = (T_trig + walk*dt) / m
m_est = round(T_trig / (npts * dt_lab));
if frac > 0.99 && span > 0.9 * npts
    m = max(1, m_est);
    fprintf(['-> copeak in every segment and ramps fill the window, so ' ...
             'the recurrence IS the window: m = %d\n'], m);
else
    m = 1;
    fprintf(['-> copeak missing from some segments, so the recurrence is ' ...
             'longer than the window: m = 1\n']);
end

%% report
f_beat = m / (T_trig + walk * dt_lab);

fprintf('\nfitted over %d segments, residual rms %.3f samples\n', ...
        numel(s), rms(resid));
fprintf('  (uniform noise over %d samples would give %.1f - if the\n', ...
        npts, npts / sqrt(12));
fprintf('   residual is near that, the fit is meaningless)\n\n');
fprintf('walk          %+.5f samples/segment (%+.2f ns lab/segment)\n', ...
        walk, walk * dt_lab * 1e9);
fprintf('wrap period   %.1f segments\n', npts / abs(walk));
fprintf('true beat     %.4f Hz   (m = %d)\n', f_beat, m);
fprintf('setpoint      %.4f Hz\n', trigger_rate);
fprintf('ratio         %.6f  <- a clean 2.000 or 0.500 means a divider\n', ...
        f_beat / trigger_rate);
fprintf('error         %+.2f Hz  (%+.0f ppm)\n', ...
        f_beat - m * trigger_rate, ...
        1e6 * (f_beat - m * trigger_rate) / (m * trigger_rate));

% A flat residual means a static frequency error - a loop sitting at the
% wrong value. Curvature or a wandering rate means the beat itself is
% moving, which is an unlocked loop instead.
figure;
subplot(2,1,1);
plot(s, u, '.', s, polyval(p, s), '-');
xlabel('segment'); ylabel('unwrapped copeak index');
title(sprintf('walk %+.4f samples/segment -> beat %.2f Hz (m=%d)', ...
      walk, f_beat, m));
subplot(2,1,2);
plot(s, resid, '.');
xlabel('segment'); ylabel('residual (samples)');
title('flat = static error; curved = the beat is moving');
