#ifndef CAMINFORMATION_H
#define CAMINFORMATION_H

#include <QWidget>

namespace Ui {
class CamInformation;
}

class CamInformation : public QWidget
{
    Q_OBJECT

public:
    explicit CamInformation(QWidget *parent = 0);
    ~CamInformation();

    void updateInformation();

protected:
    void paintEvent(QPaintEvent *event);

private:
    Ui::CamInformation *ui;
};

#endif // CAMINFORMATION_H
