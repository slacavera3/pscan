#ifndef CAMIMAGEADJUSTMENT_H
#define CAMIMAGEADJUSTMENT_H

#include <QWidget>

#include "camobject.h"

namespace Ui {
class CamImageAdjustment;
}

class CamImageAdjustment : public QWidget
{
    Q_OBJECT

public:
    explicit CamImageAdjustment(QWidget *parent = 0);
    ~CamImageAdjustment();

    void initBasicAdjustmentRange();
    void initColorAdjustmentRange();

    void updateBasicAdjustmentValue();
    void updateColorAdjustmentValue();

    void UpdateAutoLevelPercent();
    void updateATLevelsState();
    void updateColorTemperature();
    void updateColorTemperatureValue(int cctValue, bool isSetTrackBar);
    void updateATWhiteBalanceState();

protected:
    void paintEvent(QPaintEvent *event);

    void timerEvent(QTimerEvent *event);

private slots:
    void on_cbATLeftLevels_clicked(bool checked);

    void on_cbATRightLevels_clicked(bool checked);

    void on_hsldLeftLevels_valueChanged(int value);

    void on_hsldRightLevels_valueChanged(int value);

    void on_hsldGamma_valueChanged(int value);

    void on_hsldContrast_valueChanged(int value);

    void on_hsldSharpness_valueChanged(int value);

    void on_hsldPermeability_valueChanged(int value);

    void on_cbGray_clicked(bool checked);

    void on_cbAWB_clicked(bool checked);

    void on_hsldRed_valueChanged(int value);

    void on_hsldGreen_valueChanged(int value);

    void on_hsldBlue_valueChanged(int value);

    void on_hsldColorTemperature_valueChanged(int value);

    void on_hsldSaturation_valueChanged(int value);

    void on_hsldHue_valueChanged(int value);

    void on_hsldLight_valueChanged(int value);

    void on_hsldOffset_valueChanged(int value);

    void on_hsldDPC_valueChanged(int value);

    void on_pbDefault_clicked();

    void slotUpdateLevelRange();

    void on_doubleSpinBoxPercentage_valueChanged(double arg1);

private:
    Ui::CamImageAdjustment *ui;

    bool m_isSupportCCT;

    bool m_isLeftLevelRange;
    bool m_isRightLevelRange;

    bool m_isGammaRange;
    bool m_isContrastRange;
    bool m_isSharpnessRange;
    bool m_isPermeabilityRange;
    bool m_isRedChannelsRange;
    bool m_isGreenChannelRange;
    bool m_isBlueChannelRange;
    bool m_isColorTemperatureRange;
    bool m_isSaturationRange;
    bool m_isHueRange;
    bool m_isLightRange;

    int m_autoLeftLevelsTimerId;
    int m_autoRightLevelsTimerId;
    int m_autoWhiteBalanceTimerId;

    double m_percent;

    TUCAM_CAPA_ATTR m_autoLevelsCapa;
    TUCAM_PROP_ATTR m_leftLevelProp;
    TUCAM_PROP_ATTR m_AutoLevelPercentProp;
    TUCAM_PROP_ATTR m_rightLevelProp;
    TUCAM_PROP_ATTR m_gammaProp;
    TUCAM_PROP_ATTR m_contrastProp;
    TUCAM_PROP_ATTR m_sharpnessProp;
    TUCAM_PROP_ATTR m_offsetProp;
    TUCAM_PROP_ATTR m_dpcProp;
    TUCAM_PROP_ATTR m_permeabilityProp;

    TUCAM_CAPA_ATTR m_awbCapa;
    TUCAM_PROP_ATTR m_redChannelProp;
    TUCAM_PROP_ATTR m_greenChannelProp;
    TUCAM_PROP_ATTR m_blueChannelProp;
    TUCAM_PROP_ATTR m_colorTemperatureProp;
    TUCAM_PROP_ATTR m_saturationProp;
    TUCAM_PROP_ATTR m_hueProp;
    TUCAM_PROP_ATTR m_lightProp;
};

#endif // CAMIMAGEADJUSTMENT_H
