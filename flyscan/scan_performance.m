% Fly-scan performance against stage speed.
%
% Extends the original model with two things it was missing:
%   1. transfer time, which scales with copeak count and currently dominates
%   2. the speed above which one copeak smears further than half a spot,
%      which binning cannot recover
%
% Everything else is unchanged: copeaks are the native samples, the pixel
% grid is a binning choice, and SNR goes as sqrt(copeaks per pixel).

clear
close all

% ---------------------------------------------------------------- ASOPS
delay_rate = 10e3;              % asops delay rate (Hz)
T = 1/delay_rate;               % asops period (s)

% ------------------------------------------------------------- geometry
desired_stepsize_nm = 1000;     % nm - the point-by-point step we compare to
stage_speed = linspace(0.1e-3,100e-3,100);   % m/s
X = 100e-6;                     % travel range for x axis (m)
spot_um = 1.0;                  % optical resolution (um) - MEASURE THIS

% ------------------------------------------------- acquisition and link
points_per_segment = 500;       % half the 12.5 ns window at 10 MS/s
bytes_per_sample   = 2;         % 12-bit codes transfer as int16
transfer_MBps      = 4;         % ASSUMPTION - time a real download_line()

%% core model (unchanged)
t_range = X./stage_speed;       % s - flight time across the line
num_copeaks = t_range./T;
eff_pix_size_nm = 1e9.*X./num_copeaks;      % == 1e9 * stage_speed * T
eff_averages_perpix = desired_stepsize_nm./eff_pix_size_nm;
rsnr = sqrt(eff_averages_perpix);           % relative snr

%% what the original model left out
% Transfer scales with the number of copeaks, so it scales the same way as
% flight time - which is why the ratio between them is a constant and the
% curve only shifts, it does not change shape.
transfer_time = num_copeaks .* points_per_segment .* bytes_per_sample ...
                ./ (transfer_MBps*1e6);
total_line_time = t_range + transfer_time;

transfer_ratio = (points_per_segment*bytes_per_sample) ...
                 / (T * transfer_MBps*1e6);
fprintf('transfer is %.2fx the flight time, so lines take %.2fx longer\n', ...
        transfer_ratio, 1+transfer_ratio);

% Speed above which one copeak covers more than half a resolution element.
% Past this the data is smeared, not merely coarse, and no amount of
% binning in post recovers it.
v_max = (spot_um*1e-6/2) / T;   % m/s
fprintf('spot-limited ceiling: %.2f mm/s for a %.2f um spot\n', ...
        v_max*1e3, spot_um);

% Below one copeak per desired pixel the grid cannot even be filled, so
% rsnr there is a comparison, not an averaging gain.
undersampled = eff_averages_perpix < 1;

%% plots
figure; hold on; box on
plot(stage_speed.*1e3, eff_pix_size_nm./1000, 'LineWidth', 2);
yline(spot_um, '--', 'spot size', 'LineWidth', 1.5);
yline(spot_um/2, ':', 'half spot', 'LineWidth', 1.5);
xline(v_max*1e3, '--r', sprintf('%.1f mm/s', v_max*1e3), 'LineWidth', 1.5);
xlabel('stage speed (mm/s)'); ylabel('effective pixel size (\mum)');
title('native sampling - above the red line, binning cannot help');

figure; hold on; box on
plot(stage_speed.*1e3, t_range.*1000, '--', 'LineWidth', 1.5);
plot(stage_speed.*1e3, total_line_time.*1000, 'LineWidth', 2);
xline(v_max*1e3, '--r', 'LineWidth', 1.5);
legend('flight only (original)', 'flight + transfer', 'Location', 'northeast');
xlabel('stage speed (mm/s)'); ylabel('line scan time (ms)');
title(sprintf('transfer adds %.1fx', transfer_ratio));

figure; hold on; box on
plot(stage_speed.*1e3, rsnr, 'LineWidth', 2);
plot(stage_speed(undersampled).*1e3, rsnr(undersampled), 'r', 'LineWidth', 2);
xline(v_max*1e3, '--r', 'LineWidth', 1.5);
yline(1, ':', 'LineWidth', 1.5);
xlabel('stage speed (mm/s)'); ylabel('relative snr (au)');
title('red: fewer than one copeak per desired pixel');

% The trade that actually matters: SNR against total time, with the
% unusable region marked.
figure; hold on; box on
usable = ~undersampled & (stage_speed <= v_max);
plot(total_line_time.*1000, rsnr, ':', 'LineWidth', 1.5);
plot(total_line_time(usable).*1000, rsnr(usable), 'LineWidth', 2);
xlabel('line scan time (ms)'); ylabel('relative snr (au)');
title('the real trade-off, usable region solid');
set(gca,'XScale','log');
