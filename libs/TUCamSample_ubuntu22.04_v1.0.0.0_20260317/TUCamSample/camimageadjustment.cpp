#include "camimageadjustment.h"
#include "ui_camimageadjustment.h"

#include <QPainter>

CamImageAdjustment::CamImageAdjustment(QWidget *parent) :
    QWidget(parent),
    ui(new Ui::CamImageAdjustment),
    m_isSupportCCT(false),
    m_isLeftLevelRange(false),
    m_isRightLevelRange(false),
    m_isGammaRange(false),
    m_isContrastRange(false),
    m_isSharpnessRange(false),
    m_isPermeabilityRange(false),
    m_isRedChannelsRange(false),
    m_isGreenChannelRange(false),
    m_isBlueChannelRange(false),
    m_isColorTemperatureRange(false),
    m_isSaturationRange(false),
    m_isHueRange(false),
    m_isLightRange(false),
    m_autoLeftLevelsTimerId(0),
    m_autoRightLevelsTimerId(0),
    m_autoWhiteBalanceTimerId(0),
    m_percent(0)
{
    ui->setupUi(this);
    this->setLayout(ui->gridLayout);
    this->setMaximumHeight(450);

    initBasicAdjustmentRange();
    initColorAdjustmentRange();

    updateBasicAdjustmentValue();
    updateColorAdjustmentValue();
    ui->groupBox_2->setHidden(true);
}

CamImageAdjustment::~CamImageAdjustment()
{
    delete ui;
}

void CamImageAdjustment::initBasicAdjustmentRange()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    // Get auto levels
    m_autoLevelsCapa.nValDft = 0;
    m_autoLevelsCapa.nValMin = 0;
    m_autoLevelsCapa.nValMax = 0;
    m_autoLevelsCapa.nValStep = 0;
    m_autoLevelsCapa.idCapa = TUIDC_ATLEVELS;
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetAttr(hIdxTUCam, &m_autoLevelsCapa))
    {
        ui->labnAuto->setEnabled(true);
        ui->cbATLeftLevels->setEnabled(true);
        ui->cbATRightLevels->setEnabled(true);
    }
    else
    {
        ui->labnAuto->setEnabled(false);
        ui->cbATLeftLevels->setEnabled(false);
        ui->cbATRightLevels->setEnabled(false);
    }

    // Get auto level percent range
    m_AutoLevelPercentProp.dbValDft = 0;
    m_AutoLevelPercentProp.dbValMin = 0;
    m_AutoLevelPercentProp.dbValMax = 0;
    m_AutoLevelPercentProp.dbValStep = 0;
    m_AutoLevelPercentProp.nIdxChn = 0;
    m_AutoLevelPercentProp.idProp = TUIDP_ATLEVEL_PERCENTAGE;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_AutoLevelPercentProp))
    {
        ui->doubleSpinBoxPercentage->setSingleStep(0.1);
        ui->doubleSpinBoxPercentage->setSuffix(" %");
        ui->doubleSpinBoxPercentage->blockSignals(true);
        ui->doubleSpinBoxPercentage->setRange(m_AutoLevelPercentProp.dbValMin / 100.0, m_AutoLevelPercentProp.dbValMax / 100.0);
        ui->doubleSpinBoxPercentage->blockSignals(false);
    }
    else
    {
        ui->labelPercentage->setEnabled(false);
        ui->doubleSpinBoxPercentage->setEnabled(false);
    }

    // Get left level range
    m_leftLevelProp.dbValDft = 0;
    m_leftLevelProp.dbValMin = 0;
    m_leftLevelProp.dbValMax = 0;
    m_leftLevelProp.dbValStep = 0;
    m_leftLevelProp.nIdxChn = 0;
    m_leftLevelProp.idProp = TUIDP_LFTLEVELS;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_leftLevelProp))
    {
        ui->hsldLeftLevels->setEnabled(true);
        ui->labnLeftLevels->setEnabled(true);
        ui->labvLeftLevels->setEnabled(true);

        ///m_isLeftLevelRange = true;
        ui->hsldLeftLevels->blockSignals(true);
        ui->hsldLeftLevels->setRange((int)m_leftLevelProp.dbValMin, (int)m_leftLevelProp.dbValMax);
        ui->hsldLeftLevels->blockSignals(false);
    }
    else
    {
        ui->hsldLeftLevels->setEnabled(false);
        ui->labnLeftLevels->setEnabled(false);
        ui->labvLeftLevels->setEnabled(false);
    }

    // Get right level range
    m_rightLevelProp.dbValDft = 0;
    m_rightLevelProp.dbValMin = 0;
    m_rightLevelProp.dbValMax = 0;
    m_rightLevelProp.dbValStep = 0;
    m_rightLevelProp.nIdxChn = 0;
    m_rightLevelProp.idProp = TUIDP_RGTLEVELS;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_rightLevelProp))
    {
        ui->hsldRightLevels->setEnabled(true);
        ui->labnRightLevels->setEnabled(true);
        ui->labvRightLevels->setEnabled(true);

        ///m_isRightLevelRange = true;
        ui->hsldRightLevels->blockSignals(true);
        ui->hsldRightLevels->setRange((int)m_rightLevelProp.dbValMin, (int)m_rightLevelProp.dbValMax);
        ui->hsldRightLevels->blockSignals(false);
    }
    else
    {
        ui->hsldRightLevels->setEnabled(false);
        ui->labnRightLevels->setEnabled(false);
        ui->labvRightLevels->setEnabled(false);
    }

    // Get gamma range
    m_gammaProp.dbValDft = 0;
    m_gammaProp.dbValMin = 0;
    m_gammaProp.dbValMax = 0;
    m_gammaProp.dbValStep = 0;
    m_gammaProp.nIdxChn = 0;
    m_gammaProp.idProp = TUIDP_GAMMA;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_gammaProp))
    {
        ui->hsldGamma->setEnabled(true);
        ui->labnGamma->setEnabled(true);
        ui->labvGamma->setEnabled(true);

        m_isGammaRange = true;
        ui->hsldGamma->setRange((int)m_gammaProp.dbValMin, (int)m_gammaProp.dbValMax);
    }
    else
    {
        ui->hsldGamma->setEnabled(false);
        ui->labnGamma->setEnabled(false);
        ui->labvGamma->setEnabled(false);
    }


    // Get contrast range
    m_contrastProp.dbValDft = 0;
    m_contrastProp.dbValMin = 0;
    m_contrastProp.dbValMax = 0;
    m_contrastProp.dbValStep = 0;
    m_contrastProp.nIdxChn = 0;
    m_contrastProp.idProp = TUIDP_CONTRAST;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_contrastProp))
    {
        ui->hsldContrast->setEnabled(true);
        ui->labnContrast->setEnabled(true);
        ui->labvContrast->setEnabled(true);

        m_isContrastRange = true;
        ui->hsldContrast->setRange((int)m_contrastProp.dbValMin, (int)m_contrastProp.dbValMax);
    }
    else
    {
        ui->hsldContrast->setEnabled(false);
        ui->labnContrast->setEnabled(false);
        ui->labvContrast->setEnabled(false);
    }

    // Get sharpness range
    m_sharpnessProp.dbValDft = 0;
    m_sharpnessProp.dbValMin = 0;
    m_sharpnessProp.dbValMax = 0;
    m_sharpnessProp.dbValStep = 0;
    m_sharpnessProp.nIdxChn = 0;
    m_sharpnessProp.idProp = TUIDP_SHARPNESS;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_sharpnessProp))
    {
        ui->hsldSharpness->setEnabled(true);
        ui->labnSharpness->setEnabled(true);
        ui->labvSharpness->setEnabled(true);

        m_isSharpnessRange = true;
        ui->hsldSharpness->setRange((int)m_sharpnessProp.dbValMin, (int)m_sharpnessProp.dbValMax);
    }
    else
    {
        ui->hsldSharpness->setEnabled(false);
        ui->labnSharpness->setEnabled(false);
        ui->labvSharpness->setEnabled(false);
    }

    // Get offset range
    m_offsetProp.dbValDft = 0;
    m_offsetProp.dbValMin = 0;
    m_offsetProp.dbValMax = 0;
    m_offsetProp.dbValStep = 0;
    m_offsetProp.nIdxChn = 0;
    m_offsetProp.idProp = TUIDP_BLACKLEVEL;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_offsetProp))
    {
        ui->hsldOffset->setEnabled(true);
        ui->labnOffset->setEnabled(true);
        ui->labvOffset->setEnabled(true);

        ui->hsldOffset->blockSignals(true);
        ui->hsldOffset->setRange((int)m_offsetProp.dbValMin, (int)m_offsetProp.dbValMax);
        ui->hsldOffset->blockSignals(false);
    }
    else
    {
        ui->hsldOffset->setEnabled(false);
        ui->labnOffset->setEnabled(false);
        ui->labvOffset->setEnabled(false);
    }

    // Get dpc range
    m_dpcProp.dbValDft = 0;
    m_dpcProp.dbValMin = 0;
    m_dpcProp.dbValMax = 0;
    m_dpcProp.dbValStep = 0;
    m_dpcProp.nIdxChn = 0;
    m_dpcProp.idProp = TUIDP_DPCLEVEL;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_dpcProp))
    {
        ui->hsldDPC->setEnabled(true);
        ui->labnDPC->setEnabled(true);
        ui->labvDPC->setEnabled(true);

        ui->hsldDPC->blockSignals(true);
        ui->hsldDPC->setRange((int)m_dpcProp.dbValMin, (int)m_dpcProp.dbValMax);
        ui->hsldDPC->blockSignals(false);
    }
    else
    {
        ui->hsldDPC->setEnabled(false);
        ui->labnDPC->setEnabled(false);
        ui->labvDPC->setEnabled(false);
    }

    // Get permeability range
    m_permeabilityProp.dbValDft = 0;
    m_permeabilityProp.dbValMin = 0;
    m_permeabilityProp.dbValMax = 0;
    m_permeabilityProp.dbValStep = 0;
    m_permeabilityProp.nIdxChn = 0;
    m_permeabilityProp.idProp = TUIDP_SHARPNESS;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_permeabilityProp))
    {
        ui->hsldPermeability->setEnabled(true);
        ui->labnPermeability->setEnabled(true);
        ui->labvPermeability->setEnabled(true);

        m_isPermeabilityRange = true;
        ui->hsldPermeability->setRange((int)m_permeabilityProp.dbValMin, (int)m_permeabilityProp.dbValMax);
    }
    else
    {
        ui->hsldPermeability->setEnabled(false);
        ui->labnPermeability->setEnabled(false);
        ui->labvPermeability->setEnabled(false);
    }
}

void CamImageAdjustment::initColorAdjustmentRange()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    // Get white balance
    m_awbCapa.nValDft = 0;
    m_awbCapa.nValMin = 0;
    m_awbCapa.nValMax = 0;
    m_awbCapa.nValStep = 0;
    m_awbCapa.idCapa = TUIDC_ATWBALANCE;
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetAttr(hIdxTUCam, &m_awbCapa))
    {
        ui->cbAWB->setEnabled(true);
    }
    else
    {
        ui->cbAWB->setEnabled(false);
    }

    // Get red channel range
    m_redChannelProp.nIdxChn = 1;
    m_redChannelProp.dbValDft = 0;
    m_redChannelProp.dbValMin = 0;
    m_redChannelProp.dbValMax = 0;
    m_redChannelProp.dbValStep = 0;
    m_redChannelProp.idProp = TUIDP_CHNLGAIN;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_redChannelProp))
    {
        ui->hsldRed->setEnabled(true);
        ui->labnRed->setEnabled(true);
        ui->labvRed->setEnabled(true);

        m_isRedChannelsRange = true;
        ui->hsldRed->setRange((int)m_redChannelProp.dbValMin, (int)m_redChannelProp.dbValMax);
    }
    else
    {
        ui->hsldRed->setEnabled(false);
        ui->labnRed->setEnabled(false);
        ui->labvRed->setEnabled(false);
    }

    // Get green channel range
    m_greenChannelProp.nIdxChn = 2;
    m_greenChannelProp.dbValDft = 0;
    m_greenChannelProp.dbValMin = 0;
    m_greenChannelProp.dbValMax = 0;
    m_greenChannelProp.dbValStep = 0;
    m_greenChannelProp.idProp = TUIDP_CHNLGAIN;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_greenChannelProp))
    {
        ui->hsldGreen->setEnabled(true);
        ui->labnGreen->setEnabled(true);
        ui->labvGreen->setEnabled(true);

        m_isGreenChannelRange = true;
        ui->hsldGreen->setRange((int)m_greenChannelProp.dbValMin, (int)m_greenChannelProp.dbValMax);
    }
    else
    {
        ui->hsldGreen->setEnabled(false);
        ui->labnGreen->setEnabled(false);
        ui->labvGreen->setEnabled(false);
    }

    // Get blue channel range
    m_blueChannelProp.nIdxChn = 3;
    m_blueChannelProp.dbValDft = 0;
    m_blueChannelProp.dbValMin = 0;
    m_blueChannelProp.dbValMax = 0;
    m_blueChannelProp.dbValStep = 0;
    m_blueChannelProp.idProp = TUIDP_CHNLGAIN;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_blueChannelProp))
    {
        ui->hsldBlue->setEnabled(true);
        ui->labnBlue->setEnabled(true);
        ui->labvBlue->setEnabled(true);

        m_isBlueChannelRange = true;
        ui->hsldBlue->setRange((int)m_blueChannelProp.dbValMin, (int)m_blueChannelProp.dbValMax);
    }
    else
    {
        ui->hsldBlue->setEnabled(false);
        ui->labnBlue->setEnabled(false);
        ui->labvBlue->setEnabled(false);
    }

    // Get color temperature range
    m_colorTemperatureProp.nIdxChn = 0;
    m_colorTemperatureProp.dbValDft = 0;
    m_colorTemperatureProp.dbValMin = 0;
    m_colorTemperatureProp.dbValMax = 0;
    m_colorTemperatureProp.dbValStep = 0;
    m_colorTemperatureProp.idProp = TUIDP_CLRTEMPERATURE;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_colorTemperatureProp))
    {
        m_isSupportCCT = true;

        ui->hsldColorTemperature->setEnabled(true);
        ui->labnColorTemperature->setEnabled(true);
        ui->labvColorTemperature->setEnabled(true);

        m_isColorTemperatureRange = true;
        ui->hsldColorTemperature->setRange((int)m_colorTemperatureProp.dbValMin, (int)m_colorTemperatureProp.dbValMax);
    }
    else
    {
        m_isSupportCCT = false;

        ui->hsldColorTemperature->setEnabled(false);
        ui->labnColorTemperature->setEnabled(false);
        ui->labvColorTemperature->setEnabled(false);
    }

    // Get saturation range
    m_saturationProp.nIdxChn = 0;
    m_saturationProp.dbValDft = 0;
    m_saturationProp.dbValMin = 0;
    m_saturationProp.dbValMax = 0;
    m_saturationProp.dbValStep = 0;
    m_saturationProp.idProp = TUIDP_SATURATION;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_saturationProp))
    {
        ui->hsldSaturation->setEnabled(true);
        ui->labnSaturation->setEnabled(true);
        ui->labvSaturation->setEnabled(true);

        m_isSaturationRange = true;
        ui->hsldSaturation->setRange((int)m_saturationProp.dbValMin, (int)m_saturationProp.dbValMax);
    }
    else
    {
        ui->hsldSaturation->setEnabled(false);
        ui->labnSaturation->setEnabled(false);
        ui->labvSaturation->setEnabled(false);
    }

    // Get hue range
    m_hueProp.nIdxChn = 0;
    m_hueProp.dbValDft = 0;
    m_hueProp.dbValMin = 0;
    m_hueProp.dbValMax = 0;
    m_hueProp.dbValStep = 0;
    m_hueProp.idProp = TUIDP_HUE;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_hueProp))
    {
        ui->hsldHue->setEnabled(true);
        ui->labnHue->setEnabled(true);
        ui->labvHue->setEnabled(true);

        m_isHueRange = true;
        ui->hsldHue->setRange((int)m_hueProp.dbValMin, (int)m_hueProp.dbValMax);
    }
    else
    {
        ui->hsldHue->setEnabled(false);
        ui->labnHue->setEnabled(false);
        ui->labvHue->setEnabled(false);
    }

    // Get light range
    m_lightProp.nIdxChn = 0;
    m_lightProp.dbValDft = 0;
    m_lightProp.dbValMin = 0;
    m_lightProp.dbValMax = 0;
    m_lightProp.dbValStep = 0;
    m_lightProp.idProp = TUIDP_HUE;
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetAttr(hIdxTUCam, &m_lightProp))
    {
        ui->hsldLight->setEnabled(true);
        ui->labnLight->setEnabled(true);
        ui->labvLight->setEnabled(true);

        m_isLightRange = true;
        ui->hsldLight->setRange((int)m_lightProp.dbValMin, (int)m_lightProp.dbValMax);
    }
    else
    {
        ui->hsldLight->setEnabled(false);
        ui->labnLight->setEnabled(false);
        ui->labvLight->setEnabled(false);
    }
}

void CamImageAdjustment::UpdateAutoLevelPercent()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;


    double percent = 0;
    TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_ATLEVEL_PERCENTAGE, &percent, 0);
    m_percent = percent / 100.0;
    ui->doubleSpinBoxPercentage->blockSignals(true);
    ui->doubleSpinBoxPercentage->setValue(m_percent);
    ui->doubleSpinBoxPercentage->blockSignals(false);
}

void CamImageAdjustment::updateATLevelsState()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    double paramValue = 0;
    int autoLevelsState = 0;

    // Get current auto levels status
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetValue(hIdxTUCam, TUIDC_ATLEVELS, &autoLevelsState))
    {
        // Auto left levels
        if (0 != (autoLevelsState & 0x01))
        {
            if (0 == m_autoLeftLevelsTimerId)
            {
                m_autoLeftLevelsTimerId = this->startTimer(500);
            }

            ui->cbATLeftLevels->setChecked(true);

            ui->hsldLeftLevels->setEnabled(false);
            ui->labnLeftLevels->setEnabled(false);
            ui->labvLeftLevels->setEnabled(false);
        }
        else
        {
            if (0 != m_autoLeftLevelsTimerId)
            {
                killTimer(m_autoLeftLevelsTimerId);
                m_autoLeftLevelsTimerId = 0;
            }

            ui->cbATLeftLevels->setChecked(false);

            ui->hsldLeftLevels->setEnabled(true);
            ui->labnLeftLevels->setEnabled(true);
            ui->labvLeftLevels->setEnabled(true);
        }

        // Auto right levels
        if (0 != (autoLevelsState & 0x02))
        {
            if (0 == m_autoRightLevelsTimerId)
            {
                m_autoRightLevelsTimerId = this->startTimer(500);
            }

            ui->cbATRightLevels->setChecked(true);

            ui->hsldRightLevels->setEnabled(false);
            ui->labnRightLevels->setEnabled(false);
            ui->labvRightLevels->setEnabled(false);
        }
        else
        {
            if (0 != m_autoRightLevelsTimerId)
            {
                killTimer(m_autoRightLevelsTimerId);
                m_autoRightLevelsTimerId = 0;
            }

            ui->cbATRightLevels->setChecked(false);

            ui->hsldRightLevels->setEnabled(true);
            ui->labnRightLevels->setEnabled(true);
            ui->labvRightLevels->setEnabled(true);
        }
    }

    // Get current left levels
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_LFTLEVELS, &paramValue, 0))
    {
        ui->hsldLeftLevels->blockSignals(true);
        ui->hsldLeftLevels->setValue((int)paramValue);
        ui->hsldLeftLevels->blockSignals(false);
        ui->labvLeftLevels->setText(QString::number((int)paramValue));
    }

    // Get current right levels
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_RGTLEVELS, &paramValue, 0))
    {
        ui->hsldRightLevels->blockSignals(true);
        ui->hsldRightLevels->setValue((int)paramValue);
        ui->hsldRightLevels->blockSignals(false);
        ui->labvRightLevels->setText(QString::number((int)paramValue));
    }
}

void CamImageAdjustment::updateATWhiteBalanceState()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    int awbState = 0;
    int grayState = 0;
    bool isGray = false;

    // Get the gray state
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetValue(hIdxTUCam, TUIDC_MONOCHROME, &grayState))
    {
        if (1 == grayState)
        {
            isGray = true;
            ui->cbGray->setChecked(true);
            ui->cbAWB->setEnabled(false);
        }
        else
        {
            isGray = false;
            ui->cbGray->setChecked(false);
            ui->cbAWB->setEnabled(true);
        }
    }

    // Get current auto white balance status
    if (TUCAMRET_SUCCESS == TUCAM_Capa_GetValue(hIdxTUCam, TUIDC_ATWBALANCE, &awbState))
    {
        if (awbState > 1)
        {
            ui->cbAWB->setChecked(true);

            ui->hsldRed->setEnabled(false);
            ui->labnRed->setEnabled(false);
            ui->labvRed->setEnabled(false);

            ui->hsldGreen->setEnabled(false);
            ui->labnGreen->setEnabled(false);
            ui->labvGreen->setEnabled(false);

            ui->hsldBlue->setEnabled(false);
            ui->labnBlue->setEnabled(false);
            ui->labvBlue->setEnabled(false);

            ui->hsldColorTemperature->setEnabled(false);
            ui->labnColorTemperature->setEnabled(false);
            ui->labvColorTemperature->setEnabled(false);
        }
        else
        {
            ui->hsldRed->setEnabled(!isGray);
            ui->labnRed->setEnabled(!isGray);
            ui->labvRed->setEnabled(!isGray);

            ui->hsldGreen->setEnabled(!isGray);
            ui->labnGreen->setEnabled(!isGray);
            ui->labvGreen->setEnabled(!isGray);

            ui->hsldBlue->setEnabled(!isGray);
            ui->labnBlue->setEnabled(!isGray);
            ui->labvBlue->setEnabled(!isGray);

            ui->hsldColorTemperature->setEnabled(m_isSupportCCT && !isGray);
            ui->labnColorTemperature->setEnabled(m_isSupportCCT && !isGray);
            ui->labvColorTemperature->setEnabled(m_isSupportCCT && !isGray);
        }
    }

    double channelValue = 0;
    // Get the current red channel value
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_CHNLGAIN, &channelValue, 1))
    {
        ui->hsldRed->setValue((int)channelValue);
        channelValue /= 2.0f;
        ui->labvRed->setText(QString::number(channelValue, 'f', 1));
    }

    // Get the current green channel value
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_CHNLGAIN, &channelValue, 2))
    {
        ui->hsldGreen->setValue((int)channelValue);
        channelValue /= 2.0f;
        ui->labvGreen->setText(QString::number(channelValue, 'f', 1));
    }

    // Get the current blue channel value
    if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_CHNLGAIN, &channelValue, 3))
    {
        ui->hsldBlue->setValue((int)channelValue);
        channelValue /= 2.0f;
        ui->labvBlue->setText(QString::number(channelValue, 'f', 1));
    }

    updateColorTemperature();
/*
    // Get current color temperature value
    double cctValue = 0;
    if (TUCAMRET.TUCAMRET_SUCCESS == TUCamAPI.TUCAM_Prop_GetValue(opCam.hIdxTUCam, (int)TUCAM_IDPROP.TUIDP_CLRTEMPERATURE, ref cctValue, 0))
    {
        UpdateColorTemperatureValue(opCam, (int)cctValue, true);
    }
*/
}

void CamImageAdjustment::updateColorTemperatureValue(int cctValue, bool isSetTrackBar)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    char text[32] = { 0 };

    TUCAM_VALUE_TEXT valueText;
    valueText.nID = TUIDP_CLRTEMPERATURE;
    valueText.dbValue = cctValue;
    valueText.nTextSize = 32;
    valueText.pText = text;

    // Get color temperature value
    TUCAM_Prop_GetValueText(hIdxTUCam, &valueText, 0);
    ui->labvColorTemperature->setText(valueText.pText);

    if (isSetTrackBar)
    {
        ui->hsldColorTemperature->setValue(cctValue);
    }
}

void CamImageAdjustment::updateColorTemperature()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    double redChannel = 0;
    double greenChannel = 0;
    double blueChannel = 0;

    TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_CHNLGAIN, &redChannel, 1);     // Get the current red channel value
    TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_CHNLGAIN, &greenChannel, 2);   // Get the current green channel value
    TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_CHNLGAIN, &blueChannel, 3);    // Get the current blue channel value

    uint cctValue = 0;

    if (TUCAMRET_SUCCESS == TUCAM_Index_GetColorTemperature(hIdxTUCam, (uint)redChannel, (uint)greenChannel, (uint)blueChannel, &cctValue))
    {
        updateColorTemperatureValue((int)cctValue, true);
    }
}

void CamImageAdjustment::updateBasicAdjustmentValue()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    double paramValue = 0;

   // Get the current gamma value
   if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_GAMMA, &paramValue, 0))
   {
       ui->hsldGamma->setValue((int)paramValue);
       ui->labvGamma->setText(QString::number(paramValue / 100.0f, 'f', 2));
   }

   // Get the current contrast value
   if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_CONTRAST, &paramValue, 0))
   {
       ui->hsldContrast->setValue((int)paramValue);
       ui->labvContrast->setText(QString::number((int)paramValue));
   }

   // Get the current sharpness value
   if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_SHARPNESS, &paramValue, 0))
   {
       ui->hsldSharpness->setValue((int)paramValue);
       ui->labvSharpness->setText(QString::number((int)paramValue));
   }

   // Get the current offset value
   if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_BLACKLEVEL, &paramValue, 0))
   {
       ui->hsldOffset->blockSignals(true);
       ui->hsldOffset->setValue((int)paramValue);
       ui->hsldOffset->blockSignals(false);
       ui->labvOffset->setText(QString::number((int)paramValue));
   }

   // Get the current DPC value
   if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_DPCLEVEL, &paramValue, 0))
   {
       ui->hsldDPC->blockSignals(true);
       ui->hsldDPC->setValue((int)paramValue);
       ui->hsldDPC->blockSignals(false);
       ui->labvDPC->setText(QString::number((int)paramValue));
   }

   // Get the current permeability value
   if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_ENHANCE_STRENGTH, &paramValue, 0))
   {
       ui->hsldPermeability->setValue((int)paramValue);
       ui->labvPermeability->setText(QString::number((int)paramValue));
   }

   updateATLevelsState();
   UpdateAutoLevelPercent();
}

void CamImageAdjustment::updateColorAdjustmentValue()
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

   double paramValue = 0;

   // Get the current saturation value
   if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_SATURATION, &paramValue, 0))
   {
       ui->hsldSaturation->setValue((int)paramValue);
       ui->labvSaturation->setText(QString::number((int)paramValue));
   }

   // Get the current hue value
   if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_HUE, &paramValue, 0))
   {
       ui->hsldHue->setValue((int)paramValue);
       ui->labvHue->setText(QString::number((int)paramValue));
   }

   // Get the current light value
   if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_LIGHT, &paramValue, 0))
   {
       ui->hsldLight->setValue((int)paramValue);
       ui->labvLight->setText(QString::number((int)paramValue));
   }

   updateATWhiteBalanceState();
}

void CamImageAdjustment::paintEvent(QPaintEvent *event)
{
    Q_UNUSED(event);

    QPainter painter(this);
    QRect drawRect(0, 0, this->geometry().width(), this->geometry().height());
    drawRect.setTop(drawRect.top() + 24);
    drawRect.setRight(drawRect.right() - 1);
    drawRect.setBottom(drawRect.bottom() - 2);
    painter.drawRect(drawRect);
}

void CamImageAdjustment::timerEvent(QTimerEvent *event)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    double paramValue = 0.0f;

    if (event->timerId() == m_autoLeftLevelsTimerId)
    {
        if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_LFTLEVELS, &paramValue, 0))
        {
            ui->hsldLeftLevels->blockSignals(true);
            ui->hsldLeftLevels->setValue((int)paramValue);
            ui->hsldLeftLevels->blockSignals(false);
            ui->labvLeftLevels->setText(QString::number((int)paramValue));
        }
    }
    else if (event->timerId() == m_autoRightLevelsTimerId)
    {
        if (TUCAMRET_SUCCESS == TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_RGTLEVELS, &paramValue, 0))
        {
            ui->hsldRightLevels->blockSignals(true);
            ui->hsldRightLevels->setValue((int)paramValue);
            ui->hsldRightLevels->blockSignals(false);
            ui->labvRightLevels->setText(QString::number((int)paramValue));
        }
    }

}

void CamImageAdjustment::on_cbATLeftLevels_clicked(bool checked)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    int autoLeftLevels = checked ? 1 : 0;
    int autoRightLevels = ui->cbATRightLevels->isChecked() ? 1 : 0;
    int autoLevels = (autoRightLevels << 1) | autoLeftLevels;

    if (0 != autoLevels)
    {
        TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_HISTC, 1);
    }
    else
    {
        TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_HISTC, 0);
    }

    TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_ATLEVELS, autoLevels);

    if (checked)
    {
        if (0 == m_autoLeftLevelsTimerId)
        {
            m_autoLeftLevelsTimerId = this->startTimer(500);
        }

        ui->hsldLeftLevels->setEnabled(false);
        ui->labnLeftLevels->setEnabled(false);
        ui->labvLeftLevels->setEnabled(false);
    }
    else
    {
        if (0 != m_autoLeftLevelsTimerId)
        {
            killTimer(m_autoLeftLevelsTimerId);
            m_autoLeftLevelsTimerId = 0;
        }

        ui->hsldLeftLevels->setEnabled(true);
        ui->labnLeftLevels->setEnabled(true);
        ui->labvLeftLevels->setEnabled(true);

        TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_LFTLEVELS, m_leftLevelProp.dbValDft, 0);
        ui->hsldLeftLevels->blockSignals(true);
        ui->hsldLeftLevels->setValue((int)m_leftLevelProp.dbValDft);
        ui->hsldLeftLevels->blockSignals(false);
        ui->labvLeftLevels->setText(QString::number((int)m_leftLevelProp.dbValDft));
    }
}

void CamImageAdjustment::on_cbATRightLevels_clicked(bool checked)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    int autoLeftLevels = ui->cbATRightLevels->isChecked() ? 1 : 0;
    int autoRightLevels = checked ? 1 : 0;
    int autoLevels = (autoRightLevels << 1) | autoLeftLevels;

    if (0 != autoLevels)
    {
        TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_HISTC, 1);
    }
    else
    {
        TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_HISTC, 0);
    }

    TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_ATLEVELS, autoLevels);

    if (checked)
    {
        if (0 == m_autoRightLevelsTimerId)
        {
            m_autoRightLevelsTimerId = this->startTimer(500);
        }

        ui->hsldRightLevels->setEnabled(false);
        ui->labnRightLevels->setEnabled(false);
        ui->labvRightLevels->setEnabled(false);
    }
    else
    {
        if (0 != m_autoRightLevelsTimerId)
        {
            killTimer(m_autoRightLevelsTimerId);
            m_autoRightLevelsTimerId = 0;
        }

        ui->hsldRightLevels->setEnabled(true);
        ui->labnRightLevels->setEnabled(true);
        ui->labvRightLevels->setEnabled(true);

        TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_RGTLEVELS, m_rightLevelProp.dbValDft, 0);
        ui->hsldRightLevels->blockSignals(true);
        ui->hsldRightLevels->setValue((int)m_rightLevelProp.dbValDft);
        ui->hsldRightLevels->blockSignals(false);
        ui->labvRightLevels->setText(QString::number((int)m_rightLevelProp.dbValDft));
    }
}

void CamImageAdjustment::on_hsldLeftLevels_valueChanged(int value)
{
    if (m_isLeftLevelRange)
    {
        m_isLeftLevelRange = false;
        return;
    }

    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    int leftLevel = value;
    int righttLevel = ui->hsldRightLevels->value();

    if (leftLevel >= righttLevel)
    {
        righttLevel = leftLevel + 1;
        ui->hsldRightLevels->setValue(righttLevel);
        ui->labvRightLevels->setText(QString::number(righttLevel));
    }

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_LFTLEVELS, (double)leftLevel, 0);
    ui->labvLeftLevels->setText(QString::number(leftLevel));
}

void CamImageAdjustment::on_hsldRightLevels_valueChanged(int value)
{
    if (m_isRightLevelRange)
    {
        m_isRightLevelRange = false;
        return;
    }

    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    int leftLevel = ui->hsldLeftLevels->value();
    int righttLevel = value;

    if (leftLevel >= righttLevel)
    {
        leftLevel = righttLevel - 1;
        leftLevel = leftLevel < 0 ? 0 : leftLevel;
        ui->hsldLeftLevels->setValue(leftLevel);
        ui->labvLeftLevels->setText(QString::number(leftLevel));
    }

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_RGTLEVELS, (double)righttLevel, 0);
    ui->labvRightLevels->setText(QString::number(righttLevel));
}

void CamImageAdjustment::on_hsldGamma_valueChanged(int value)
{
    if (m_isGammaRange)
    {
        m_isGammaRange = false;
        return;
    }

    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    double gammaValue = value / 100.0f;
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_GAMMA, (double)value, 0);
    ui->labvGamma->setText(QString::number(gammaValue, 'f', 2));
}

void CamImageAdjustment::on_hsldContrast_valueChanged(int value)
{
    if (m_isContrastRange)
    {
        m_isContrastRange = false;
        return;
    }

    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_CONTRAST, (double)value, 0);
    ui->labvContrast->setText(QString::number(value));
}

void CamImageAdjustment::on_hsldSharpness_valueChanged(int value)
{
    if (m_isSharpnessRange)
    {
        m_isSharpnessRange = false;
        return;
    }

    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_SHARPNESS, (double)value, 0);
    ui->labvSharpness->setText(QString::number(value));
}

void CamImageAdjustment::on_hsldPermeability_valueChanged(int value)
{
    if (m_isPermeabilityRange)
    {
        m_isPermeabilityRange = false;
        return;
    }

    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_ENHANCE_STRENGTH, (double)value, 0);
    ui->labvPermeability->setText(QString::number(value));
}

void CamImageAdjustment::on_cbGray_clicked(bool checked)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_MONOCHROME, checked ? 1 : 0);

    updateATWhiteBalanceState();
}

void CamImageAdjustment::on_cbAWB_clicked(bool checked)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    if (checked)
    {
        TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_ATWBALANCE, 2);

        if (0 == m_autoWhiteBalanceTimerId)
        {
            m_autoWhiteBalanceTimerId = this->startTimer(500);
        }
    }
    else
    {
        TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_ATWBALANCE, 0);

        if (0 != m_autoWhiteBalanceTimerId)
        {
            killTimer(m_autoWhiteBalanceTimerId);
            m_autoWhiteBalanceTimerId = 0;
        }
    }

    updateATWhiteBalanceState();
}

void CamImageAdjustment::on_hsldRed_valueChanged(int value)
{
    if (m_isRedChannelsRange)
    {
        m_isRedChannelsRange = false;
        return;
    }

    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_CHNLGAIN, (double)value, 1);
    ui->labvRed->setText(QString::number(value / 2.0f, 'f', 1));

    updateColorTemperature();
}

void CamImageAdjustment::on_hsldGreen_valueChanged(int value)
{
    if (m_isGreenChannelRange)
    {
        m_isGreenChannelRange = false;
        return;
    }

    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_CHNLGAIN, (double)value, 2);
    ui->labvGreen->setText(QString::number(value / 2.0f, 'f', 1));

    updateColorTemperature();
}

void CamImageAdjustment::on_hsldBlue_valueChanged(int value)
{
    if (m_isBlueChannelRange)
    {
        m_isBlueChannelRange = false;
        return;
    }

    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_CHNLGAIN, (double)value, 3);
    ui->labvBlue->setText(QString::number(value / 2.0f, 'f', 1));

    updateColorTemperature();
}

void CamImageAdjustment::on_hsldColorTemperature_valueChanged(int value)
{
    if (m_isColorTemperatureRange)
    {
        m_isColorTemperatureRange = false;
        return;
    }

    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_CLRTEMPERATURE, (double)value, 0);
    updateColorTemperatureValue(value, false);

    double channelValue = 0.0f;
    TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_CHNLGAIN, &channelValue, 1);
    ui->hsldRed->setValue((int)channelValue);
    ui->labvRed->setText(QString::number(channelValue / 2.0f, 'f', 1));

    TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_CHNLGAIN, &channelValue, 2);
    ui->hsldGreen->setValue((int)channelValue);
    ui->labvGreen->setText(QString::number(channelValue / 2.0f, 'f', 1));

    TUCAM_Prop_GetValue(hIdxTUCam, TUIDP_CHNLGAIN, &channelValue, 3);
    ui->hsldBlue->setValue((int)channelValue);
    ui->labvBlue->setText(QString::number(channelValue / 2.0f, 'f', 1));
}

void CamImageAdjustment::on_hsldSaturation_valueChanged(int value)
{
    if (m_isSaturationRange)
    {
        m_isSaturationRange = false;
        return;
    }

    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_SATURATION, (double)value, 0);
    ui->labvSaturation->setText(QString::number(value));
}

void CamImageAdjustment::on_hsldHue_valueChanged(int value)
{
    if (m_isHueRange)
    {
        m_isHueRange = false;
        return;
    }

    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_HUE, (double)value, 0);
    ui->labvHue->setText(QString::number(value));
}

void CamImageAdjustment::on_hsldLight_valueChanged(int value)
{
    if (m_isLightRange)
    {
        m_isLightRange = false;
        return;
    }

    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_LIGHT, (double)value, 0);
    ui->labvLight->setText(QString::number(value));
}

void CamImageAdjustment::on_hsldOffset_valueChanged(int value)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_BLACKLEVEL, (double)value, 0);
    ui->labvOffset->setText(QString::number(value));
}

void CamImageAdjustment::on_hsldDPC_valueChanged(int value)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_DPCLEVEL, (double)value, 0);
    ui->labvDPC->setText(QString::number(value));
}

void CamImageAdjustment::on_pbDefault_clicked()
{
    bool isWaiting = CamObject::getInstance()->isWaitting();

    if (isWaiting)
    {
        CamObject::getInstance()->stopWaittingForFrames();
    }

    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_ATLEVELS, m_autoLevelsCapa.nValDft);
    TUCAM_Capa_SetValue(hIdxTUCam, TUIDC_ATWBALANCE, m_awbCapa.nValDft);

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_ATLEVEL_PERCENTAGE, m_AutoLevelPercentProp.dbValDft, 0);
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_LFTLEVELS, m_leftLevelProp.dbValDft, 0);
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_RGTLEVELS, m_rightLevelProp.dbValDft, 0);
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_GAMMA, m_gammaProp.dbValDft, 0);
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_CONTRAST, m_contrastProp.dbValDft, 0);
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_SHARPNESS, m_sharpnessProp.dbValDft, 0);
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_ENHANCE_STRENGTH, m_permeabilityProp.dbValDft, 0);

    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_CHNLGAIN, m_redChannelProp.dbValDft, 0);
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_CHNLGAIN, m_greenChannelProp.dbValDft, 0);
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_CHNLGAIN, m_blueChannelProp.dbValDft, 0);
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_SATURATION, m_saturationProp.dbValDft, 0);
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_HUE, m_hueProp.dbValDft, 0);
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_LIGHT, m_lightProp.dbValDft, 0);
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_DPCLEVEL, m_dpcProp.dbValDft, 0);
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_BLACKLEVEL, m_offsetProp.dbValDft, 0);

    updateBasicAdjustmentValue();
    updateColorAdjustmentValue();

    if (isWaiting)
    {
        CamObject::getInstance()->startWaittingForFrames();
    }

}

void CamImageAdjustment::slotUpdateLevelRange()
{
    initBasicAdjustmentRange();
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_LFTLEVELS, m_leftLevelProp.dbValDft, 0);
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_RGTLEVELS, m_rightLevelProp.dbValDft, 0);
    updateBasicAdjustmentValue();
}

void CamImageAdjustment::on_doubleSpinBoxPercentage_valueChanged(double arg1)
{
    HDTUCAM hIdxTUCam = CamObject::getInstance()->getCamHandle();

    if (NULL == hIdxTUCam)
        return;

    int value = (int)(arg1 * 100);
    if (value >= 5 && m_percent < arg1)
    {
        value = (int)((arg1 + 0.02) * 100);
    }
    TUCAM_Prop_SetValue(hIdxTUCam, TUIDP_ATLEVEL_PERCENTAGE, value, 0);
    UpdateAutoLevelPercent();
}
